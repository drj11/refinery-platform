cft = CloudFormationTemplate(description="A very small template.")

user_data_script = open('bootstrap.sh').read()

cft.resources.ec2_instance = Resource('AnInstance', 'AWS::EC2::Instance',
    Properties({
        'ImageId': 'ami-d05e75b8',
        'InstanceType': 'm3.medium',
        'UserData': base64(user_data_script),
        'KeyName': 'id_rsa',
    })
)
