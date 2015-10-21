#!/bin/bash

export DEBIAN_FRONTEND=noninteractive
/usr/bin/apt-get -qq update && /usr/bin/apt-get -q -y install make puppet git ruby-dev
mkdir /vagrant && chown ubuntu:ubuntu /vagrant
su ubuntu -c "/usr/bin/git clone https://github.com/parklab/refinery-platform.git /vagrant"
/usr/bin/gem install librarian-puppet -v 2.2.1
su ubuntu -c "cd /vagrant/deployment && /usr/local/bin/librarian-puppet install"
/usr/bin/puppet apply --modulepath=/vagrant/deployment/modules /vagrant/deployment/manifests/default.pp
