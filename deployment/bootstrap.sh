#!/usr/bin/env bash

export DEBIAN_FRONTEND=noninteractive
/usr/bin/apt-get -qq update && /usr/bin/apt-get -q -y install make puppet git ruby-dev
/usr/sbin/addgroup --gid 1002 refinery
/usr/sbin/useradd -u 1002 -c "Refinery application user" -m -g refinery refinery
/usr/bin/gem install librarian-puppet -v 2.2.1 --no-rdoc --no-ri
su refinery -c "cd /srv/refinery-platform/deployment && /usr/local/bin/librarian-puppet install"
