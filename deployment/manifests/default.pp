$app_user = "refinery"
$app_group = "refinery"
$virtualenv = "/home/${app_user}/.virtualenvs/refinery-platform"
$project_root = "/srv/refinery-platform"
$django_root = "${project_root}/refinery"
$requirements = "${project_root}/requirements.txt"
$django_settings_module = "config.settings.dev"
$ui_app_root = "${django_root}/ui"

# to make logs easier to read
class { 'timezone':
  timezone => 'America/New_York',
}

# for better performance
sysctl { 'vm.swappiness': value => '10' }

file { "/home/${app_user}/.ssh":
  ensure => directory,
  owner => $app_user,
  group => $app_group,
  mode => 0700,
}
->
file { "/home/${app_user}/.ssh/config":
  ensure => file,
  source => "/srv/refinery-platform/deployment/ssh-config",
  owner => $app_user,
  group => $app_group,
}

class { 'postgresql::globals':
  version => '9.3',
  encoding => 'UTF8',
  locale => 'en_US.utf8',
}
class { 'postgresql::server':
}
class { 'postgresql::lib::devel':
}
postgresql::server::role { $app_user:
  createdb => true,
}
->
postgresql::server::db { 'refinery':
  user => $app_user,
  password => '',
  owner => $app_user,
}

class { 'python':
  version => 'system',
  pip => true,
  dev => true,
  virtualenv => true,
  gunicorn => false,
}

class venvdeps {
  #TODO: peg packages to specific versions
  package { 'build-essential': }
  package { 'libncurses5-dev': }
  package { 'libldap2-dev': }
  package { 'libsasl2-dev': }
  package { 'libffi-dev': }  # for SSL modules
}
include venvdeps

file { "/home/${app_user}/.virtualenvs":
  # workaround for parent directory ~${app_user}/.virtualenvs does not exist error
  ensure => directory,
  owner => $app_user,
  group => $app_group,
}
->
python::virtualenv { $virtualenv:
  ensure => present,
  owner => $app_user,
  group => $app_group,
  require => [ Class['venvdeps'], Class['postgresql::lib::devel'] ],
}
~>
python::requirements { $requirements:
  virtualenv => $virtualenv,
  owner => $app_user,
  group => $app_group,
}

package { 'virtualenvwrapper': }
->
file_line { "virtualenvwrapper_config":
  path => "/home/${app_user}/.profile",
  line => "source /etc/bash_completion.d/virtualenvwrapper",
  require => Python::Virtualenv[$virtualenv],
}
->
file { "virtualenvwrapper_project":
  # workaround for setvirtualenvproject command not found
  ensure => file,
  path => "${virtualenv}/.project",
  content => "${django_root}",
  owner => $app_user,
  group => $app_group,
}

file { ["/srv/refinery-platform/isa-tab", "/srv/refinery-platform/import", "/srv/refinery-platform/static"]:
  ensure => directory,
  owner => $app_user,
  group => $app_group,
}

file_line { "django_settings_module":
  path => "/home/${app_user}/.profile",
  line => "export DJANGO_SETTINGS_MODULE=${django_settings_module}",
}
->
file { "${django_root}/config/config.json":
  ensure => file,
  source => "${django_root}/config/config.json.sample",
  owner => $app_user,
  group => $app_group,
  replace => false,
}
->
exec { "syncdb":
  command => "${virtualenv}/bin/python ${django_root}/manage.py syncdb --migrate --noinput",
  environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
  user => $app_user,
  group => $app_group,
  require => [
               Python::Requirements[$requirements],
               Postgresql::Server::Db["refinery"]
             ],
}
->
exec { "create_superuser":
  command => "${virtualenv}/bin/python ${django_root}/manage.py loaddata superuser.json",
  environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
  user => $app_user,
  group => $app_group,
}
->
exec { "init_refinery":
  command => "${virtualenv}/bin/python ${django_root}/manage.py init_refinery 'Refinery' '192.168.50.50:8000'",
  environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
  user => $app_user,
  group => $app_group,
}
->
exec { "create_user":
  command => "${virtualenv}/bin/python ${django_root}/manage.py create_user 'guest' 'guest' 'guest@example.com' 'Guest' '' ''",
  environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
  user => $app_user,
  group => $app_group,
}
->
exec {
  "build_core_schema":
    command => "${virtualenv}/bin/python ${django_root}/manage.py build_solr_schema --using=core > solr/core/conf/schema.xml",
    environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
    cwd => $django_root,
    user => $app_user,
    group => $app_group;
  "build_data_set_manager_schema":
    command => "${virtualenv}/bin/python ${django_root}/manage.py build_solr_schema --using=data_set_manager > solr/data_set_manager/conf/schema.xml",
    environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
    cwd => $django_root,
    user => $app_user,
    group => $app_group;
}

class solr {
  $solr_version = "5.3.1"
  $solr_archive = "solr-${solr_version}.tgz"
  $solr_url = "http://archive.apache.org/dist/lucene/solr/${solr_version}/${solr_archive}"

  package { 'java':
    name => 'openjdk-7-jdk',
  }

  exec { "solr_wget":
    command => "wget ${solr_url} -O /usr/src/${solr_archive}",
    creates => "/usr/src/${solr_archive}",
    path => "/usr/bin:/bin",
    timeout => 600,  # downloading can take a long time
  }
  ->
  exec { "solr_unpack_installer":
    command => "mkdir -p /opt && tar -xzf /usr/src/${solr_archive} -C /usr/src solr-${solr_version}/bin/install_solr_service.sh --strip-components=2",
    creates => "/opt/solr-${solr_version}",
    path => "/usr/bin:/bin",
  }
  ->
  exec { "solr_install_as_service":
    command => "sudo bash /usr/src/install_solr_service.sh /usr/src/${solr_archive} -d /vagrant/refinery/solr_app_data -u vagrant && chown -R ${appuser}:${appuser} /opt/solr-${solr_version}",
    creates => "/opt/solr-${solr_version}",
    path => "/usr/bin:/bin",
  }
  ->
  file { "/opt/solr":
    ensure => link,
    target => "/opt/solr-${solr_version}",
  }
  ->
  exec { "solr_stop":
    command => "sudo service solr stop",
    path => "/usr/bin:/bin",
    returns => [0, 1]
  }
  ->
  file_line { "solr_config_pid":
    path => "/vagrant/refinery/solr_app_data/solr.in.sh",
    line => "SOLR_PID_DIR=/vagrant/refinery/solr_app_data",
    match => "^SOLR_PID_DIR"
  }
  ->
  file_line { "solr_config_home":
    path => "/vagrant/refinery/solr_app_data/solr.in.sh",
    line => "SOLR_HOME=/vagrant/refinery/solr/",
    match => "^SOLR_HOME"
  }
  ->
  file_line { "solr_config_log4j":
    path => "/vagrant/refinery/solr_app_data/solr.in.sh",
    line => "LOG4J_PROPS=/vagrant/refinery/solr/log4j.properties",
    match => "^LOG4J_PROPS"
  }
  ->
  file_line { "solr_config_log_dir":
    path => "/vagrant/refinery/solr_app_data/solr.in.sh",
    line => "SOLR_LOGS_DIR=/vagrant/refinery/log",
    match => "^SOLR_LOGS_DIR"
  }
}
include solr

class neo4j {
  $neo4j_version = "2.3.0"
  $neo4j_name = "neo4j-community-${neo4j_version}"
  $neo4j_archive = "${neo4j_name}-unix.tar.gz"
  $neo4j_url = "http://neo4j.com/artifact.php?name=${neo4j_archive}"

  exec { "neo4j_wget":
    command => "wget ${neo4j_url} -O /usr/src/${neo4j_archive}",
    creates => "/usr/src/${neo4j_archive}",
    path => "/usr/bin:/bin",
    timeout => 600,  # downloading can take a long time
  }
  ->
  exec { "neo4j_unpack":
    command => "mkdir -p /opt && tar -xzf /usr/src/${neo4j_archive} -C /opt && chown -R ${app_user}:${app_group} /opt/${neo4j_name}",
    creates => "/opt/${neo4j_name}",
    path => "/usr/bin:/bin",
  }
  ->
  file { "/opt/neo4j":
    ensure => link,
    target => "${neo4j_name}",
  }
  ->
  file { "/opt/neo4j-data":
    ensure => 'directory',
    owner  => 'vagrant',
    group  => 'vagrant',
  }
  ->
  file_line { "neo4j_graph_db_location":
    path => "/opt/${neo4j_name}/conf/neo4j-server.properties",
    line => "org.neo4j.server.database.location=/opt/neo4j-data/graph.db",
    match => "^org.neo4j.server.database.location=",
  }
  ->
  file_line { "neo4j_no_authentication":
    path => "/opt/${neo4j_name}/conf/neo4j-server.properties",
    line => "dbms.security.auth_enabled=false",
    match => "^dbms.security.auth_enabled=",
  }
  ->
  file_line { "neo4j_all_ips":
    path => "/opt/${neo4j_name}/conf/neo4j-server.properties",
    line => "org.neo4j.server.webserver.address=0.0.0.0",
    match => "^#org.neo4j.server.webserver.address=",
  }
  limits::fragment {
    "${app_user}/soft/nofile":
      value => "40000";
    "${app_user}/hard/nofile":
      value => "40000";
  }
}
include neo4j

class owl2neo4j {
  $owl2neo4j_version = "0.3.3"
  $owl2neo4j_url = "https://github.com/flekschas/owl2neo4j/releases/download/v${owl2neo4j_version}/owl2neo4j.jar"

  # Need to remove the old file manually as wget throws a weird
  # `HTTP request sent, awaiting response... 403 Forbidden` error when the file
  # already exists.

  exec { "owl2neo4j_wget":
    command => "rm -f /opt/owl2neo4j.jar && wget -P /opt/ ${owl2neo4j_url}",
    creates => "/opt/owl2neo4j",
    path => "/usr/bin:/bin",
    timeout => 120,  # downloading can take some time
  }
}
include owl2neo4j

class rabbit {
  package { 'curl': }
  ->
  class { '::rabbitmq':
    package_ensure => installed,
    service_ensure => running,
    port => '5672',
  }
}
include rabbit

class ui {
  # need a version of Node that's more recent than one included with Ubuntu 12.04
  # apt::ppa { 'ppa:chris-lea/node.js': }
  include apt

  apt::source { 'nodejs':
    ensure      => 'present',
    comment     => 'Nodesource NodeJS repo.',
    location    => 'https://deb.nodesource.com/node_4.x',
    release     => 'trusty',
    repos       => 'main',
    key         => '9FD3B784BC1C6FC31A8A0A1C1655A0AB68576280',
    key_server  => 'keyserver.ubuntu.com',
    include_src => true,
    include_deb => true
  }

  package { 'nodejs':
    name => 'nodejs',
    ensure  => latest,
    require => Apt::Source['nodejs']
  }
  ->
  package {
    'bower': ensure => '1.6.5', provider => 'npm';
    'grunt-cli': ensure => '0.1.13', provider => 'npm';
  }
  ->
  exec { "npm_local_modules":
    command => "/usr/bin/npm prune && /usr/bin/npm install",
    cwd => $ui_app_root,
    logoutput => on_failure,
    user => $app_user,
    group => $app_group,
  }
  ->
  exec { "bower_modules":
    # remove bower_components instead?
    command => "/usr/bin/bower prune && /usr/bin/bower install --config.interactive=false",
    cwd => $ui_app_root,
    logoutput => on_failure,
    user => $app_user,
    group => $app_group,
    environment => ["HOME=/home/${app_user}"],
  }
  ->
  exec { "grunt":
    command => "/usr/bin/grunt",
    cwd => $ui_app_root,
    logoutput => on_failure,
    user => $app_user,
    group => $app_group,
  }
  ->
  exec { "collectstatic":
    command => "${virtualenv}/bin/python ${django_root}/manage.py collectstatic --clear --noinput",
    environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
    user => $app_user,
    group => $app_group,
    require => Python::Requirements[$requirements],
  }
}
include ui

file { "${django_root}/supervisord.conf":
  ensure => file,
  source => "${django_root}/supervisord.conf.sample",
  owner => $app_user,
  group => $app_group,
}
->
exec { "supervisord":
  command => "${virtualenv}/bin/supervisord",
  environment => ["DJANGO_SETTINGS_MODULE=${django_settings_module}"],
  cwd => $django_root,
  creates => "/tmp/supervisord.pid",
  user => $app_user,
  group => $app_group,
  require => [ Class["ui"], Class["solr"], Class["neo4j"], Class ["rabbit"] ],
}

package { 'libapache2-mod-wsgi': }
package { 'apache2': }
exec { 'apache2-wsgi':
  command => '/usr/sbin/a2enmod wsgi',
  subscribe => [ Package['apache2'], Package['libapache2-mod-wsgi'] ],
}
->
file { "/etc/apache2/sites-available/001-refinery.conf":
  ensure => file,
  content => template("/srv/refinery-platform/deployment/apache.conf"),
}
~>
exec { 'refinery-apache2':
  command => '/usr/sbin/a2ensite 001-refinery',
}
~>
service { 'apache2':
  ensure => running,
  hasrestart => true,
}

package { 'memcached': }
service { 'memcached':
  ensure => running,
}
