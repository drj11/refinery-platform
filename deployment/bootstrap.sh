#!/usr/bin/env bash

export DEBIAN_FRONTEND=noninteractive

/usr/bin/apt-get -qq update
OPERATIONS="htop"
DEVELOPMENT="make puppet git ruby-dev"
/usr/bin/apt-get -q -y install $OPERATIONS $DEVELOPMENT

/usr/bin/gem install librarian-puppet -v 2.2.1 --no-rdoc --no-ri
