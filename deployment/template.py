cft = CloudFormationTemplate(description="Single instance template.")

user_data_script = open('bootstrap.sh').read() + (
    "/usr/bin/puppet apply "
    "--modulepath=/srv/refinery-platform/deployment/modules "
    "/srv/refinery-platform/deployment/manifests/default.pp")

# add to user_data_script
# in production: git clone
# in development: rsync

cft.resources.ec2_instance = Resource(
    'AnInstance', 'AWS::EC2::Instance',
    Properties({
        'ImageId': 'ami-d05e75b8',
        'InstanceType': 'm3.medium',
        'UserData': base64(user_data_script),
        'KeyName': 'id_rsa',
    })
)
