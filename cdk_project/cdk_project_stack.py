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

class MyLAMPStackCDK(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create a VPC with optimized NAT configuration
        vpc = ec2.Vpc(
            self, "MyVpc",
            max_azs=2, # Spanning across 2 Availability Zones
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

                # for each AZ, create  the above SubnetConfiguration
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

        # RDS Proxy Security Group
        rds_proxy_sg = ec2.SecurityGroup(
            self, "RdsProxySecurityGroup",
            vpc=vpc,
            description="RDS Proxy Security Group",
            allow_all_outbound=True  # Allow outbound to RDS
        )

        # ECS Egress Rules
        ecs_sg.add_egress_rule(
            peer=rds_proxy_sg,
            connection=ec2.Port.tcp(3306),
            description="Allow MySQL access via RDS Proxy"
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

        # RDS Proxy → RDS
        rds_sg.add_ingress_rule(
            rds_proxy_sg,
            ec2.Port.tcp(3306),
            "Allow MySQL from RDS Proxy"
        )

        # ECS → RDS Proxy
        rds_proxy_sg.add_ingress_rule(
            ecs_sg,
            ec2.Port.tcp(3306),
            "Allow MySQL from ECS"
        )

        # Database Secret
        db_secret = secretsmanager.Secret(
            self, "DBSecret",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "admin"}',
                # explicit setting value for  'username'

                generate_string_key="password",
                # auto generate a random password for the key 'password'
                
                exclude_punctuation=True,
                include_space=False
            )

            # expected output
             #{
            #"username": "admin",
          #"password": "aB3dE4fG5hJ6kL7mN"
            
        # }

        )

        # RDS Instance
        db_instance = rds.DatabaseInstance(
            self, "MyRDS",
            engine=rds.DatabaseInstanceEngine.mysql(version=rds.MysqlEngineVersion.VER_8_0_36),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE3, ec2.InstanceSize.MICRO),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            credentials=rds.Credentials.from_secret(db_secret),
            allocated_storage=20, #in GIGABYTES
            database_name="kstu",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            security_groups=[rds_sg],
            cloudwatch_logs_exports=["error", "slowquery"],  # Enable only error and slow query logs
            cloudwatch_logs_retention=logs.RetentionDays.ONE_MONTH,

        
        )

        # RDS Proxy
        rds_proxy = db_instance.add_proxy(
            "RdsProxy",
            vpc=vpc,
            secrets=[db_secret],
            security_groups=[rds_proxy_sg],
            debug_logging=True,
            require_tls=False  # Set to True for production

            # This ensures secure connections between the  application and the RDS Proxy.
            #  If a client does not support TLS, the connection will be rejected.
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

            # allows tasks to be able to pull images and write logs to cloudwatch
        )
        execution_role.add_to_policy(
            aws_iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[db_secret.secret_arn]
            )

            # allows tasks to get values of secrets from secrets manager
        )

        # Task Definition
        task_definition = ecs.FargateTaskDefinition(
            self, "TaskDef",
            execution_role=execution_role,
            cpu=512,
            memory_limit_mib=1024

            # cpu=512 → This is 512 CPU units, which equals 0.5 vCPU (½ vCPU) in AWS Fargate.
            # memory_limit_mib=1024 → This is 1024 MiB (Megabytes in Binary), which equals 1 GB of RAM.

        )

        # ECR Repository
        ecr_repo = ecr.Repository.from_repository_name(
            self, "ECRRepo",
            repository_name="kstu-src-app-repo"

            # this must be created before deploying this infrastructure
        )

        # Container Definition
        container = task_definition.add_container(
            "AppContainer",
            image=ecs.ContainerImage.from_ecr_repository(ecr_repo, "latest"),
            memory_limit_mib=512, # in MiB
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="ecs",
                log_retention=logs.RetentionDays.ONE_MONTH
            ),
            environment={
                "DB_CONNECTION": "mysql",
                "DB_HOST": rds_proxy.endpoint,  # Use RDS Proxy endpoint
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

            # similar to -> docker run -p <host_port>:<container_port> my_container

        )

        # ECS Service
        service = ecs.FargateService(
            self, "EcsService",
            enable_execute_command=True,  # Enable ECS Exec
            cluster=cluster,
            task_definition=task_definition,
            security_groups=[ecs_sg],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            desired_count=2,
            health_check_grace_period=Duration.minutes(3)  # Allow time for app startup
        )

        # Auto-scaling
        scaling = service.auto_scale_task_count(
            min_capacity=2,  # Minimum number of tasks
            max_capacity=5   # Maximum number of tasks
        )
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=70,  # Scale out when CPU > 70%
        )
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
                path="/ref",  # The endpoint used for health checks (e.g., /ref)
                interval=Duration.seconds(60),  # Health check runs every 60s
                healthy_threshold_count=2,  # Marks the target as healthy after 2 successful checks
                unhealthy_threshold_count=3,  # Marks the target as unhealthy after 3 failures
                timeout=Duration.seconds(10),  # The check must complete within 10s
                healthy_http_codes="200"  # Only 200 responses mean the target is healthy
            )
        )

        # Outputs
        cdk.CfnOutput(self, "ALBURL", value=alb.load_balancer_dns_name)
        
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "EcsServiceName",
            value=service.service_name,
            description="The name of the ECS Service"
        )


# Where cloudwatch is used
# 1. ecs
# 2. rds proxy
# 3. rds


# How to migrate the tables for the app

# 1. List all ECS clusters
#  > aws ecs list-clusters

# 2. Describe the selected cluster to confirm details
# aws ecs describe-clusters --clusters <your-cluster-name>


# 3. List all running tasks in the selected cluster
# aws ecs list-tasks --cluster <your-cluster-name>

# 4. Run a Bash shell inside a running ECS task
# aws ecs execute-command \
#     --cluster <your-cluster-name> \
#     --task <task-id> \
#     --container <container-name-from-cdk-or-aws-console> \
#     --command "/bin/bash" \
#     --interactive

# Example
# aws ecs execute-command \
#     --cluster MyLAMPStackCDK-EcsCluster97242B84-9Iw5IWf44b4r \
#     --task 1a7d2ec3a0c2402b99c0cb68ae3b29e7 \
#     --container AppContainer \
#     --command "/bin/bash" \
#     --interactive


# after deployment to ECS, update jenkins url path before submitting auto-builds

# updating jenkins url path
# sudo nano /var/lib/jenkins/jenkins.model.JenkinsLocationConfiguration.xml
# sudo systemctl restart jenkins

# also got to the repo where the LAMP stack is deployed and update the jenkins url path under webhooks of this repo's settings

# dont forget  to update the Jenkins file's     ECS_CLUSTER = "<CLUSTER_NAME>" and  # ECS_SERVICE = "<SERVICE_NAME>"  each time you deploy your infrastructure