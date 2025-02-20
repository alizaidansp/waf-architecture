from aws_cdk import (
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_iam,
    aws_rds as rds,
    aws_secretsmanager as secretsmanager,
    Stack,
    aws_logs as logs,
    Duration
)
import aws_cdk as cdk
from constructs import Construct
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_applicationautoscaling as appscaling

class MyNewInfStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create a VPC with optimized NAT configuration
        vpc = ec2.Vpc(
            self, "MyVpc",
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                ),
            ]
        )

        # Security Groups
        # -------------------------------
        # ALB Security Group (Public facing)
        alb_sg = ec2.SecurityGroup(
            self, "AlbSecurityGroup",
            vpc=vpc,
            description="Allow HTTP access to ALB",
            allow_all_outbound=True  # ALB needs outbound to ECS
        )

        # ECS Security Group (Private)
        ecs_sg = ec2.SecurityGroup(
            self, "EcsSecurityGroup",
            vpc=vpc,
            description="ECS Security Group",
            allow_all_outbound=False  # Restrict egress
        )

        # RDS Security Group
        rds_sg = ec2.SecurityGroup(
            self, "RdsSecurityGroup",
            vpc=vpc,
            description="RDS Security Group",
            allow_all_outbound=False  # RDS shouldn't initiate connections
        )

        # ECS Egress Rules
        ecs_sg.add_egress_rule(
            peer=rds_sg,
            connection=ec2.Port.tcp(3306),
            description="Allow MySQL access"
        )

        ecs_sg.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="Allow ECR/Secrets Manager access"
        )

        # ALB Ingress
        alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(80),
            "Allow HTTP"
        )

        # ALB → ECS
        ecs_sg.add_ingress_rule(
            alb_sg,
            ec2.Port.tcp(80),
            "Allow from ALB"
        )

        # ECS → RDS
        rds_sg.add_ingress_rule(
            ecs_sg,
            ec2.Port.tcp(3306),
            "Allow MySQL"
        )

      

        

        # Database Secret
        db_secret = secretsmanager.Secret(
            self, "DBSecret",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "admin"}',
                generate_string_key="password",
                exclude_punctuation=True,
                include_space=False
            )
        )

        # RDS Instance
        db_instance = rds.DatabaseInstance(
            self, "MyRDS",
            engine=rds.DatabaseInstanceEngine.mysql(version=rds.MysqlEngineVersion.VER_8_0_36),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            credentials=rds.Credentials.from_secret(db_secret),
            allocated_storage=20,
            database_name="kstu",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            security_groups=[rds_sg]
        )

        # ECS Cluster
        cluster = ecs.Cluster(
        self, "EcsCluster",
        vpc=vpc,
        execute_command_configuration=ecs.ExecuteCommandConfiguration(
            logging=ecs.ExecuteCommandLogging.DEFAULT
        )
    )


        # Task Execution Role
        execution_role = aws_iam.Role(
            self, "ExecutionRole",
            assumed_by=aws_iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                aws_iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
            ]
        )
        execution_role.add_to_policy(
            aws_iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[db_secret.secret_arn]
            )
        )

        # Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self, "TaskDef",
            execution_role=execution_role,
            cpu=512,
            memory_limit_mib=1024
        )

        # ECR Repository
        ecr_repo = ecr.Repository.from_repository_name(
            self, "ECRRepo",
            repository_name="kstu-src-app-repo"
        )

        # Container Definition
        container = task_definition.add_container(
            "AppContainer",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, "latest"),
            memory_limit_mib=512,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="ecs",
                log_retention=logs.RetentionDays.ONE_MONTH
            ),
            environment={
                "DB_CONNECTION": "mysql",
                "DB_HOST": db_instance.db_instance_endpoint_address,
                "DB_PORT": "3306",
                "DB_DATABASE": "kstu",
            },
            secrets={
                "DB_USERNAME": ecs.Secret.from_secrets_manager(db_secret, "username"),
                "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password")
            }
        )
        container.add_port_mappings(
        ecs.PortMapping(
            container_port=80,
            host_port=80,
            protocol=ecs.Protocol.TCP
        )
    )

        # ECS Service
        service = ecs.FargateService(
            self, "EcsService",
            enable_execute_command=True , # Enable ECS Exec
            cluster=cluster,
            task_definition=task_definition,
            security_groups=[ecs_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            desired_count=2,
            health_check_grace_period=Duration.minutes(3)  # Allow time for app startup
        )

        # auto-scaling

        # Create an Auto Scaling Target
        scaling = service.auto_scale_task_count(
            min_capacity=2,  # Minimum number of tasks
            max_capacity=5   # Maximum number of tasks
        )

        # Scale out when CPU usage is high
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=70,  # Scale out when CPU > 70%
        )

        # Scale out when memory usage is high
        scaling.scale_on_memory_utilization(
            "MemoryScaling",
            target_utilization_percent=75,  # Scale out when Memory > 75%
        )


          # Load Balancer
        alb = elbv2.ApplicationLoadBalancer(
            self, "EcsALB",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg
        )

        # Listener and Target Group
        listener = alb.add_listener("HttpListener", port=80, open=True)
       
        listener.add_targets(
        "EcsTarget",
        port=80,
        targets=[service],
        health_check=elbv2.HealthCheck(
            path="/ref",  # Use your /ref endpoint
            interval=Duration.seconds(60),
            healthy_threshold_count=2,
            unhealthy_threshold_count=3,
            timeout=Duration.seconds(10),
            healthy_http_codes="200"  # Only 200 is considered healthy
        )
    )

        

        # Outputs
        cdk.CfnOutput(self, "RDSHost", value=db_instance.db_instance_endpoint_address)
        cdk.CfnOutput(self, "DBSecretArn", value=db_secret.secret_arn)
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "EcsServiceURL", value=alb.load_balancer_dns_name)
        