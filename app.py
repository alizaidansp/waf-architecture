#!/usr/bin/env python3
import os

import aws_cdk as cdk

from cdk_project.cdk_project_stack import MyLAMPStackCDK

app = cdk.App()
MyLAMPStackCDK(app, "MyLAMPStackCDK")



app.synth()

