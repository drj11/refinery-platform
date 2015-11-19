#!/usr/bin/env bash

export DEBIAN_FRONTEND=noninteractive

/usr/bin/apt-get -qq update
OPERATIONS="htop"
DEVELOPMENT="make puppet git ruby-dev"
/usr/bin/apt-get -q -y install $OPERATIONS $DEVELOPMENT

/usr/bin/gem install librarian-puppet -v 2.2.1 --no-rdoc --no-ri
mkdir /srv/refinery-platform
chown ubuntu:ubuntu /srv/refinery-platform
sudo su -c 'git clone https://github.com/parklab/refinery-platform.git /srv/refinery-platform' ubuntu
cd /srv/refinery-platform/deployment && /usr/local/bin/librarian-puppet install
