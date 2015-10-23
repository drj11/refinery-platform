#!/usr/bin/env bash

APP_USER=refinery
APP_GROUP=$APP_USER

export DEBIAN_FRONTEND=noninteractive
/usr/bin/apt-get -qq update && /usr/bin/apt-get -q -y install make puppet git ruby-dev
/usr/sbin/addgroup --gid 1010 $APP_GROUP
/usr/sbin/useradd -u 1010 -g $APP_GROUP -m -c "Refinery application user" $APP_USER
/usr/bin/gem install librarian-puppet -v 2.2.1 --no-rdoc --no-ri
su $APP_USER -c "cd /srv/refinery-platform/deployment && /usr/local/bin/librarian-puppet install"
