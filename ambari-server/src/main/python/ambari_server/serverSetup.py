#!/usr/bin/env python

'''
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
'''

import optparse
import os
import re
import shutil
import sys

from ambari_commons.exceptions import FatalException
from ambari_commons.firewall import Firewall
from ambari_commons.inet_utils import force_download_file
from ambari_commons.logging_utils import get_silent, print_info_msg, print_warning_msg, print_error_msg
from ambari_commons.os_check import OSConst
from ambari_commons.os_family_impl import OsFamilyFuncImpl, OsFamilyImpl
from ambari_commons.os_utils import run_os_command, is_root
from ambari_commons.str_utils import compress_backslashes
from ambari_server.dbConfiguration import DBMSConfigFactory, check_jdbc_drivers
from ambari_server.serverConfiguration import configDefaults, JDKRelease, \
  get_ambari_properties, get_full_ambari_classpath, get_java_exe_path, get_JAVA_HOME, get_value_from_properties, \
  read_ambari_user, update_properties, validate_jdk, write_property, \
  JAVA_HOME, JAVA_HOME_PROPERTY, JCE_NAME_PROPERTY, JDBC_RCA_URL_PROPERTY, JDBC_URL_PROPERTY, \
  JDK_NAME_PROPERTY, JDK_RELEASES, NR_USER_PROPERTY, OS_FAMILY, OS_FAMILY_PROPERTY, OS_TYPE, OS_TYPE_PROPERTY, OS_VERSION, \
  RESOURCES_DIR_PROPERTY, SERVICE_PASSWORD_KEY, SERVICE_USERNAME_KEY, VIEWS_DIR_PROPERTY, get_is_secure, \
  get_is_persisted
from ambari_server.serverUtils import is_server_runing
from ambari_server.setupSecurity import adjust_directory_permissions
from ambari_server.userInput import get_YN_input, get_validated_string_input
from ambari_server.utils import locate_file




# selinux commands
GET_SE_LINUX_ST_CMD = locate_file('sestatus', '/usr/sbin')
SE_SETENFORCE_CMD = "setenforce 0"
SE_STATUS_DISABLED = "disabled"
SE_STATUS_ENABLED = "enabled"
SE_MODE_ENFORCING = "enforcing"
SE_MODE_PERMISSIVE = "permissive"

# Non-root user setup commands
NR_USER_COMMENT = "Ambari user"

VIEW_EXTRACT_CMD = "{0} -cp {1}" + \
                   "org.apache.ambari.server.view.ViewRegistry extract {2} " + \
                   "> " + configDefaults.SERVER_OUT_FILE + " 2>&1"

MAKE_FILE_EXECUTABLE_CMD = "chmod a+x {0}"

# use --no-same-owner when running as root to prevent uucp as the user (AMBARI-6478)
UNTAR_JDK_ARCHIVE = "tar --no-same-owner -xvf {0}"

JDK_PROMPT = "[{0}] {1}\n"
JDK_CUSTOM_CHOICE_PROMPT = "[{0}] - Custom JDK\n==============================================================================\nEnter choice ({1}): "
JDK_VALID_CHOICES = "^[{0}{1:d}]$"

IS_CUSTOM_JDK = False

def get_supported_jdbc_drivers():
  factory = DBMSConfigFactory()
  return factory.get_supported_jdbc_drivers()

JDBC_DB_OPTION_VALUES = get_supported_jdbc_drivers()


#
# Setup security prerequisites
#

def verify_setup_allowed():
  if get_silent():
    properties = get_ambari_properties()
    if properties == -1:
      print_error_msg("Error getting ambari properties")
      return -1

    isSecure = get_is_secure(properties)
    if isSecure:
      (isPersisted, masterKeyFile) = get_is_persisted(properties)
      if not isPersisted:
        print "ERROR: Cannot run silent 'setup' with password encryption enabled " \
              "and Master Key not persisted."
        print "Ambari Server 'setup' exiting."
        return 1
  return 0


#
# Security enhancements (Linux only)
#

#
# Checks SELinux
#

def check_selinux():
  try:
    retcode, out, err = run_os_command(GET_SE_LINUX_ST_CMD)
    se_status = re.search('(disabled|enabled)', out).group(0)
    print "SELinux status is '" + se_status + "'"
    if se_status == SE_STATUS_DISABLED:
      return 0
    else:
      try:
        se_mode = re.search('(enforcing|permissive)', out).group(0)
      except AttributeError:
        err = "Error determining SELinux mode. Exiting."
        raise FatalException(1, err)
      print "SELinux mode is '" + se_mode + "'"
      if se_mode == SE_MODE_ENFORCING:
        print "Temporarily disabling SELinux"
        run_os_command(SE_SETENFORCE_CMD)
      print_warning_msg(
        "SELinux is set to 'permissive' mode and temporarily disabled.")
      ok = get_YN_input("OK to continue [y/n] (y)? ", True)
      if not ok:
        raise FatalException(1, None)
      return 0
  except OSError:
    print_warning_msg("Could not run {0}: OK".format(GET_SE_LINUX_ST_CMD))
  return 0


# No security enhancements in Windows
@OsFamilyFuncImpl(OSConst.WINSRV_FAMILY)
def disable_security_enhancements():
  retcode = 0
  err = ''
  return (retcode, err)

@OsFamilyFuncImpl(OsFamilyImpl.DEFAULT)
def disable_security_enhancements():
  print 'Checking SELinux...'
  err = ''
  retcode = check_selinux()
  if not retcode == 0:
    err = 'Failed to disable SELinux. Exiting.'
  return (retcode, err)


#
# User account creation
#

class AmbariUserChecks(object):
  def __init__(self):
    self.NR_USER_CHANGE_PROMPT = ""
    self.NR_USER_CUSTOMIZE_PROMPT = ""
    self.NR_DEFAULT_USER = ""
    self.NR_USER_COMMENT = "Ambari user"

  def do_checks(self):
    try:
      user = read_ambari_user()
      create_user = False
      update_user_setting = False
      if user is not None:
        create_user = get_YN_input(self.NR_USER_CHANGE_PROMPT.format(user), False)
        update_user_setting = create_user  # Only if we will create another user
      else:  # user is not configured yet
        update_user_setting = True  # Write configuration anyway
        create_user = get_YN_input(self.NR_USER_CUSTOMIZE_PROMPT, False)
        if not create_user:
          user = self.NR_DEFAULT_USER

      if create_user:
        (retcode, user) = self._create_custom_user()
        if retcode != 0:
          return retcode

      if update_user_setting:
        write_property(NR_USER_PROPERTY, user)

      adjust_directory_permissions(user)
    except OSError as e:
      print_error_msg("Failed: %s" % str(e))
      return 4
    except Exception as e:
      print_error_msg("Unexpected error %s" % str(e))
      return 1
    return 0

  def _create_custom_user(self):
    pass

@OsFamilyImpl(os_family=OSConst.WINSRV_FAMILY)
class AmbariUserChecksWindows(AmbariUserChecks):
  def __init__(self):
    super(AmbariUserChecksWindows, self).__init__()

    self.NR_USER_CHANGE_PROMPT = "Ambari-server service is configured to run under user '{0}'. Change this setting [y/n] (n)? "
    self.NR_USER_CUSTOMIZE_PROMPT = "Customize user account for ambari-server service [y/n] (n)? "
    self.NR_DEFAULT_USER = "NT AUTHORITY\SYSTEM"

  def _create_custom_user(self):
    user = get_validated_string_input(
      "Enter user account for ambari-server service ({0}):".format(self.NR_DEFAULT_USER),
      self.NR_DEFAULT_USER, None,
      "Invalid username.",
      False
    )
    if user == self.NR_DEFAULT_USER:
      return 0, user
    password = get_validated_string_input("Enter password for user {0}:".format(user), "", None, "Password", True, False)

    from ambari_commons.os_windows import UserHelper

    uh = UserHelper()

    status, message = uh.create_user(user,password)
    if status == UserHelper.USER_EXISTS:
      print_info_msg("User {0} already exists, make sure that you typed correct password for user, "
                     "skipping user creation".format(user))

    elif status == UserHelper.ACTION_FAILED:  # fail
      print_warning_msg("Can't create user {0}. Failed with message {1}".format(user, message))
      return UserHelper.ACTION_FAILED, None

    # setting SeServiceLogonRight to user

    status, message = uh.add_user_privilege(user, 'SeServiceLogonRight')
    if status == UserHelper.ACTION_FAILED:
      print_warning_msg("Can't add SeServiceLogonRight to user {0}. Failed with message {1}".format(user, message))
      return UserHelper.ACTION_FAILED, None

    print_info_msg("User configuration is done.")
    print_warning_msg("When using non SYSTEM user make sure that your user have read\write access to log directories and "
                      "all server directories. In case of integrated authentication for SQL Server make sure that your "
                      "user properly configured to use ambari and metric database.")
    #storing username and password in os.environ temporary to pass them to service
    os.environ[SERVICE_USERNAME_KEY] = user
    os.environ[SERVICE_PASSWORD_KEY] = password
    return 0, user

@OsFamilyImpl(os_family=OsFamilyImpl.DEFAULT)
class AmbariUserChecksLinux(AmbariUserChecks):
  def __init__(self):
    super(AmbariUserChecksLinux, self).__init__()

    self.NR_USER_CHANGE_PROMPT = "Ambari-server daemon is configured to run under user '{0}'. Change this setting [y/n] (n)? "
    self.NR_USER_CUSTOMIZE_PROMPT = "Customize user account for ambari-server daemon [y/n] (n)? "
    self.NR_DEFAULT_USER = "root"

    self.NR_USERADD_CMD = 'useradd -M --comment "{1}" ' \
                          '--shell %s -d /var/lib/ambari-server/keys/ {0}' % locate_file('nologin', '/sbin')

  def _create_custom_user(self):
    user = get_validated_string_input(
      "Enter user account for ambari-server daemon (root):",
      "root",
      "^[a-z_][a-z0-9_-]{1,31}$",
      "Invalid username.",
      False
    )

    print_info_msg("Trying to create user {0}".format(user))
    command = self.NR_USERADD_CMD.format(user, self.NR_USER_COMMENT)
    retcode, out, err = run_os_command(command)
    if retcode == 9:  # 9 = username already in use
      print_info_msg("User {0} already exists, "
                     "skipping user creation".format(user))

    elif retcode != 0:  # fail
      print_warning_msg("Can't create user {0}. Command {1} "
                        "finished with {2}: \n{3}".format(user, command, retcode, err))
      return retcode, None

    print_info_msg("User configuration is done.")
    return 0, user

def check_ambari_user():
  return AmbariUserChecks().do_checks()


#
# Firewall
#

def check_firewall():
  firewall_obj = Firewall().getFirewallObject()
  firewall_on = firewall_obj.check_iptables()
  if firewall_obj.stderrdata and len(firewall_obj.stderrdata) > 0:
    print firewall_obj.stderrdata
  if firewall_on:
    print_warning_msg("%s is running. Confirm the necessary Ambari ports are accessible. " %
                      firewall_obj.FIREWALL_SERVICE_NAME +
                      "Refer to the Ambari documentation for more details on ports.")
    ok = get_YN_input("OK to continue [y/n] (y)? ", True)
    if not ok:
      raise FatalException(1, None)


#
#  ## JDK ###
#

class JDKSetup(object):
  def __init__(self):
    self.JDK_DEFAULT_CONFIGS = []

    self.JDK_PROMPT = "[{0}] {1}\n"
    self.JDK_CUSTOM_CHOICE_PROMPT = "[{0}] - Custom JDK\n==============================================================================\nEnter choice ({1}): "
    self.JDK_VALID_CHOICES = "^[{0}{1:d}]$"
    self.JDK_MIN_FILESIZE = 5000
    self.JAVA_BIN = ""

    self.jdk_index = 0

  #
  # Downloads and installs the JDK and the JCE policy archive
  #
  def download_and_install_jdk(self, args):
    global IS_CUSTOM_JDK
    properties = get_ambari_properties()
    if properties == -1:
      err = "Error getting ambari properties"
      raise FatalException(-1, err)

    conf_file = properties.fileName
    ok = False
    jcePolicyWarn = "JCE Policy files are required for configuring Kerberos security. If you plan to use Kerberos," \
                    "please make sure JCE Unlimited Strength Jurisdiction Policy Files are valid on all hosts."

    if args.java_home:
      if not validate_jdk(args.java_home):
        err = "Path to java home " + args.java_home + " or java binary file does not exists"
        raise FatalException(1, err)

      print_warning_msg("JAVA_HOME " + args.java_home + " must be valid on ALL hosts")
      print_warning_msg(jcePolicyWarn)
      IS_CUSTOM_JDK = True

      properties.process_pair(JAVA_HOME_PROPERTY, args.java_home)
      properties.removeOldProp(JDK_NAME_PROPERTY)
      properties.removeOldProp(JCE_NAME_PROPERTY)
      update_properties(properties)

      self._ensure_java_home_env_var_is_set(args.java_home)
      return 0

    java_home_var = get_JAVA_HOME()

    if get_silent():
      if not java_home_var:
        #No java_home_var set, detect if java is already installed
        if os.environ.has_key(JAVA_HOME):
          args.java_home = os.environ[JAVA_HOME]

          properties.process_pair(JAVA_HOME_PROPERTY, args.java_home)
          properties.removeOldProp(JDK_NAME_PROPERTY)
          properties.removeOldProp(JCE_NAME_PROPERTY)
          update_properties(properties)

          self._ensure_java_home_env_var_is_set(args.java_home)
          return 0
        else:
          # For now, changing the existing JDK to make sure we use a supported one
          pass

    if java_home_var:
      change_jdk = get_YN_input("Do you want to change Oracle JDK [y/n] (n)? ", False)
      if not change_jdk:
        self._ensure_java_home_env_var_is_set(java_home_var)
        return 0

    #Continue with the normal setup, taking the first listed JDK version as the default option
    jdk_num = str(self.jdk_index + 1)
    (jdks, jdk_choice_prompt, jdk_valid_choices, custom_jdk_number) = self._populate_jdk_configs(properties, jdk_num)

    jdk_num = get_validated_string_input(
      jdk_choice_prompt,
      jdk_num,
      jdk_valid_choices,
      "Invalid number.",
      False
    )

    if jdk_num == str(custom_jdk_number):
      IS_CUSTOM_JDK = True
      print_warning_msg("JDK must be installed on all hosts and JAVA_HOME must be valid on all hosts.")
      print_warning_msg(jcePolicyWarn)
      args.java_home = get_validated_string_input("Path to JAVA_HOME: ", None, None, None, False, False)
      if not os.path.exists(args.java_home) or not os.path.isfile(os.path.join(args.java_home, "bin", self.JAVA_BIN)):
        err = "Java home path or java binary file is unavailable. Please put correct path to java home."
        raise FatalException(1, err)
      print "Validating JDK on Ambari Server...done."

      properties.process_pair(JAVA_HOME_PROPERTY, args.java_home)
      properties.removeOldProp(JDK_NAME_PROPERTY)
      properties.removeOldProp(JCE_NAME_PROPERTY)
      update_properties(properties)

      self._ensure_java_home_env_var_is_set(args.java_home)
      return 0

    self.jdk_index = int(jdk_num) - 1
    jdk_cfg = jdks[self.jdk_index]

    try:
      resources_dir = properties[RESOURCES_DIR_PROPERTY]
    except (KeyError), e:
      err = 'Property ' + str(e) + ' is not defined at ' + conf_file
      raise FatalException(1, err)

    dest_file = os.path.abspath(os.path.join(resources_dir, jdk_cfg.dest_file))
    if os.path.exists(dest_file):
      print "JDK already exists, using " + dest_file
    else:
      ok = get_YN_input("To download the Oracle JDK and the Java Cryptography Extension (JCE) "
                        "Policy Files you must accept the "
                        "license terms found at "
                        "http://www.oracle.com/technetwork/java/javase/"
                        "terms/license/index.html and not accepting will "
                        "cancel the Ambari Server setup and you must install the JDK and JCE "
                        "files manually.\nDo you accept the "
                        "Oracle Binary Code License Agreement [y/n] (y)? ", True)
      if not ok:
        print 'Exiting...'
        sys.exit(1)

      jdk_url = jdk_cfg.url

      print 'Downloading JDK from ' + jdk_url + ' to ' + dest_file
      self._download_jdk(jdk_url, dest_file)

    try:
      (retcode, out, java_home_dir) = self._install_jdk(dest_file, jdk_cfg)
    except Exception, e:
      print "Installation of JDK has failed: %s\n" % str(e)
      file_exists = os.path.isfile(dest_file)
      if file_exists:
        ok = get_YN_input("JDK found at " + dest_file + ". "
                          "Would you like to re-download the JDK [y/n] (y)? ", not get_silent())
        if not ok:
          err = "Unable to install JDK. Please remove JDK file found at " + \
                dest_file + " and re-run Ambari Server setup"
          raise FatalException(1, err)
        else:
          jdk_url = jdk_cfg.url

          print 'Re-downloading JDK from ' + jdk_url + ' to ' + dest_file
          self._download_jdk(jdk_url, dest_file)
          print 'Successfully re-downloaded JDK distribution to ' + dest_file

          try:
            (retcode, out) = self._install_jdk(dest_file, jdk_cfg)
          except Exception, e:
            print "Installation of JDK was failed: %s\n" % str(e)
            err = "Unable to install JDK. Please remove JDK, file found at " + \
                  dest_file + " and re-run Ambari Server setup"
            raise FatalException(1, err)

      else:
        err = "Unable to install JDK. File " + dest_file + " does not exist, " \
                                                           "please re-run Ambari Server setup"
        raise FatalException(1, err)

    properties.process_pair(JDK_NAME_PROPERTY, jdk_cfg.dest_file)
    properties.process_pair(JAVA_HOME_PROPERTY, java_home_dir)

    try:
      self._download_jce_policy(jdk_cfg, resources_dir, properties)
    except FatalException, e:
      print "JCE Policy files are required for secure HDP setup. Please ensure " \
            " all hosts have the JCE unlimited strength policy 6, files."
      print_error_msg("Failed to download JCE policy files:")
      if e.reason is not None:
        print_error_msg("\nREASON: {0}".format(e.reason))
        # TODO: We don't fail installation if _download_jce_policy fails. Is it OK?

    update_properties(properties)

    self._ensure_java_home_env_var_is_set(java_home_dir)

    return 0

  def _populate_jdk_configs(self, properties, jdk_num):
    if properties.has_key(JDK_RELEASES):
      jdk_names = properties[JDK_RELEASES].split(',')
      jdks = []
      for jdk_name in jdk_names:
        jdkR = JDKRelease.from_properties(properties, jdk_name)
        jdks.append(jdkR)
    else:
      jdks = self.JDK_DEFAULT_CONFIGS

    n_config = 1
    jdk_choice_prompt = ''
    jdk_choices = ''
    for jdk in jdks:
      jdk_choice_prompt += self.JDK_PROMPT.format(n_config, jdk.desc)
      jdk_choices += str(n_config)
      n_config += 1

    jdk_choice_prompt += self.JDK_CUSTOM_CHOICE_PROMPT.format(n_config, jdk_num)
    jdk_valid_choices = self.JDK_VALID_CHOICES.format(jdk_choices, n_config)

    return (jdks, jdk_choice_prompt, jdk_valid_choices, n_config)

  def _download_jdk(self, jdk_url, dest_file):
    jdk_download_fail_msg = " Failed to download JDK: {0}. Please check that the " \
                            "JDK is available at {1}. Also you may specify JDK file " \
                            "location in local filesystem using --jdk-location command " \
                            "line argument.".format("{0}", jdk_url)
    try:
      force_download_file(jdk_url, dest_file)

      print 'Successfully downloaded JDK distribution to ' + dest_file
    except FatalException:
      raise
    except Exception, e:
      err = jdk_download_fail_msg.format(str(e))
      raise FatalException(1, err)

  def _download_jce_policy(self, jdk_cfg, resources_dir, properties):
    jcpol_url = jdk_cfg.jcpol_url
    dest_file = os.path.abspath(os.path.join(resources_dir, jdk_cfg.dest_jcpol_file))

    if not os.path.exists(dest_file):
      print 'Downloading JCE Policy archive from ' + jcpol_url + ' to ' + dest_file
      try:
        force_download_file(jcpol_url, dest_file)

        print 'Successfully downloaded JCE Policy archive to ' + dest_file
        properties.process_pair(JCE_NAME_PROPERTY, jdk_cfg.dest_jcpol_file)
      except FatalException:
        raise
      except Exception, e:
        err = 'Failed to download JCE Policy archive: ' + str(e)
        raise FatalException(1, err)
    else:
      print "JCE Policy archive already exists, using " + dest_file

  # Base implementation, overriden in the subclasses
  def _install_jdk(self, java_inst_file, java_home_dir):
    pass

  # Base implementation, overriden in the subclasses
  def _ensure_java_home_env_var_is_set(self, java_home_dir):
    pass

@OsFamilyImpl(os_family=OSConst.WINSRV_FAMILY)
class JDKSetupWindows(JDKSetup):
  def __init__(self):
    super(JDKSetupWindows, self).__init__()
    self.JDK_DEFAULT_CONFIGS = [
      JDKRelease("jdk7.67", "Oracle JDK 1.7.67",
                 "http://public-repo-1.hortonworks.com/ARTIFACTS/jdk-7u67-windows-x64.exe", "jdk-7u67-windows-x64.exe",
                 "http://public-repo-1.hortonworks.com/ARTIFACTS/UnlimitedJCEPolicyJDK7.zip", "UnlimitedJCEPolicyJDK7.zip",
                 "C:\\jdk1.7.0_67",
                 "Creating (jdk.*)/jre")
    ]

    self.JAVA_BIN = "java.exe"

  def _install_jdk(self, java_inst_file, jdk_cfg):
    jdk_inst_dir = jdk_cfg.inst_dir
    print "Installing JDK to {0}".format(jdk_inst_dir)

    if not os.path.exists(jdk_inst_dir):
      os.makedirs(jdk_inst_dir)

    if java_inst_file.endswith(".exe"):
      (dirname, filename) = os.path.split(java_inst_file)
      installLogFilePath = os.path.join(configDefaults.OUT_DIR, filename + "-install.log")
      #jre7u67.exe /s INSTALLDIR=<dir> STATIC=1 WEB_JAVA=0 /L \\var\\log\\ambari-server\\jre7u67.exe-install.log
      installCmd = [
        java_inst_file,
        "/s",
        "INSTALLDIR=" + jdk_inst_dir,
        "STATIC=1",
        "WEB_JAVA=0",
        "/L",
        installLogFilePath
      ]
      retcode, out, err = run_os_command(installCmd)
      #TODO: support .msi file installations
      #msiexec.exe jre.msi /s INSTALLDIR=<dir> STATIC=1 WEB_JAVA=0 /L \\var\\log\\ambari-server\\jre7u67-install.log ?
    else:
      err = "JDK installation failed.Unknown file mask."
      raise FatalException(1, err)

    if retcode == 1603:
      # JDK already installed
      print "JDK already installed in {0}".format(jdk_inst_dir)
      retcode = 0
    else:
      if retcode != 0:
        err = "Installation of JDK returned exit code %s" % retcode
        raise FatalException(retcode, err)

      print "Successfully installed JDK to {0}".format(jdk_inst_dir)

    # Don't forget to adjust the JAVA_HOME env var

    return (retcode, out, jdk_inst_dir)

  def _ensure_java_home_env_var_is_set(self, java_home_dir):
    if not os.environ.has_key(JAVA_HOME) or os.environ[JAVA_HOME] != java_home_dir:
      java_home_dir_unesc = compress_backslashes(java_home_dir)
      retcode, out, err = run_os_command("SETX {0} {1} /M".format(JAVA_HOME, java_home_dir_unesc))
      if retcode != 0:
        print_warning_msg("SETX output: " + out)
        print_warning_msg("SETX error output: " + err)
        err = "Setting JAVA_HOME failed. Exit code={0}".format(retcode)
        raise FatalException(1, err)

      os.environ[JAVA_HOME] = java_home_dir

@OsFamilyImpl(os_family=OsFamilyImpl.DEFAULT)
class JDKSetupLinux(JDKSetup):
  def __init__(self):
    super(JDKSetupLinux, self).__init__()
    self.JDK_DEFAULT_CONFIGS = [
      JDKRelease("jdk6.31", "Oracle JDK 1.6",
                 "http://public-repo-1.hortonworks.com/ARTIFACTS/jdk-6u31-linux-x64.bin", "jdk-6u31-linux-x64.bin",
                 "http://public-repo-1.hortonworks.com/ARTIFACTS/jce_policy-6.zip", "jce_policy-6.zip",
                 "/usr/jdk64/jdk1.6.0_31",
                 "Creating (jdk.*)/jre")
    ]

    self.JAVA_BIN = "java"

    self.CREATE_JDK_DIR_CMD = "/bin/mkdir -p {0}"
    self.MAKE_FILE_EXECUTABLE_CMD = "chmod a+x {0}"

    # use --no-same-owner when running as root to prevent uucp as the user (AMBARI-6478)
    self.UNTAR_JDK_ARCHIVE = "tar --no-same-owner -xvf {0}"

  def _install_jdk(self, java_inst_file, jdk_cfg):
    jdk_inst_dir = jdk_cfg.inst_dir
    print "Installing JDK to {0}".format(jdk_inst_dir)

    retcode, out, err = run_os_command(self.CREATE_JDK_DIR_CMD.format(jdk_inst_dir))
    savedPath = os.getcwd()
    os.chdir(jdk_inst_dir)

    try:
      if java_inst_file.endswith(".bin"):
        retcode, out, err = run_os_command(self.MAKE_FILE_EXECUTABLE_CMD.format(java_inst_file))
        retcode, out, err = run_os_command(java_inst_file + ' -noregister')
      elif java_inst_file.endswith(".gz"):
        retcode, out, err = run_os_command(self.UNTAR_JDK_ARCHIVE.format(java_inst_file))
      else:
        err = "JDK installation failed.Unknown file mask."
        raise FatalException(1, err)
    finally:
      os.chdir(savedPath)

    if retcode != 0:
      err = "Installation of JDK returned exit code %s" % retcode
      raise FatalException(retcode, err)

    jdk_version = re.search(jdk_cfg.reg_exp, out).group(1)
    java_home_dir = os.path.join(jdk_inst_dir, jdk_version)

    print "Successfully installed JDK to {0}".format(jdk_inst_dir)
    return (retcode, out, java_home_dir)

  def _ensure_java_home_env_var_is_set(self, java_home_dir):
    #No way to do this in Linux. Best we can is to set the process environment variable.
    os.environ[JAVA_HOME] = java_home_dir

def download_and_install_jdk(options):
  return JDKSetup().download_and_install_jdk(options)


#
# Configures the OS settings in ambari properties.
#
def configure_os_settings():
  properties = get_ambari_properties()
  if properties == -1:
    print_error_msg("Error getting ambari properties")
    return -1
  try:
    conf_os_type = properties[OS_TYPE_PROPERTY]
    if conf_os_type != '':
      print_info_msg("os_type already set in the properties file")
      return 0
  except (KeyError):
    print_error_msg("os_type is not set in the properties file. Setting it now.")

  # to check server/agent compatibility
  master_os_family = OS_FAMILY + OS_VERSION
  # to check supported os_types
  master_os_type = OS_TYPE + OS_VERSION

  write_property(OS_FAMILY_PROPERTY, master_os_family)
  write_property(OS_TYPE_PROPERTY, master_os_type)
  return 0


#
# JDBC
#

def _check_jdbc_options(options):
  return (options.jdbc_driver is not None and options.jdbc_db is not None)

def proceedJDBCProperties(args):
  if not os.path.isfile(args.jdbc_driver):
    err = "File {0} does not exist!".format(args.jdbc_driver)
    raise FatalException(1, err)

  if args.jdbc_db not in JDBC_DB_OPTION_VALUES:
    err = "Unsupported database name {0}. Please see help for more information.".format(args.jdbc_db)
    raise FatalException(1, err)

  _cache_jdbc_driver(args)

# No JDBC driver caching in Windows at this point. Will cache it along with the integrated authentication dll into a
#  zip archive at a later moment.
@OsFamilyFuncImpl(os_family=OSConst.WINSRV_FAMILY)
def _cache_jdbc_driver(args):
  pass

#TODO JDBC driver caching almost duplicates the LinuxDBMSConfig._install_jdbc_driver() functionality
@OsFamilyFuncImpl(os_family=OsFamilyImpl.DEFAULT)
def _cache_jdbc_driver(args):
  properties = get_ambari_properties()
  if properties == -1:
    err = "Error getting ambari properties"
    raise FatalException(-1, err)
  conf_file = properties.fileName

  try:
    resources_dir = properties[RESOURCES_DIR_PROPERTY]
  except (KeyError), e:
    err = 'Property ' + str(e) + ' is not defined at ' + conf_file
    raise FatalException(1, err)

  symlink_name = args.jdbc_db + "-jdbc-driver.jar"
  jdbc_symlink = os.path.join(resources_dir, symlink_name)
  path, jdbc_name = os.path.split(args.jdbc_driver)

  if os.path.lexists(jdbc_symlink):
    os.remove(jdbc_symlink)

  if not os.path.isfile(os.path.join(resources_dir, jdbc_name)):
    try:
      shutil.copy(args.jdbc_driver, resources_dir)
    except Exception, e:
      err = "Can not copy file {0} to {1} due to: {2} . Please check file " \
            "permissions and free disk space.".format(args.jdbc_driver, resources_dir, str(e))
      raise FatalException(1, err)

  os.symlink(os.path.join(resources_dir, jdbc_name), jdbc_symlink)
  print "JDBC driver was successfully initialized."

#
# Database
#

# Ask user for database connection properties
def prompt_db_properties(options):
  ok = False
  if options.must_set_database_options:
    ok = get_YN_input("Enter advanced database configuration [y/n] (n)? ", False)

  print 'Configuring database...'

  factory = DBMSConfigFactory()

  options.must_set_database_options = ok
  options.database_index = factory.select_dbms(options)

def _setup_database(options):
  properties = get_ambari_properties()
  if properties == -1:
    raise FatalException(-1, "Error getting ambari properties")

  factory = DBMSConfigFactory()

  dbmsAmbari = factory.create(options, properties, "Ambari")
  resultA = dbmsAmbari.configure_database(properties)

  # Now save the properties file
  if resultA:
    update_properties(properties)

    dbmsAmbari.setup_database()

def _createDefDbFactory(options):
  properties = get_ambari_properties()
  if properties == -1:
    raise FatalException(-1, "Error getting ambari properties")
  if not (properties.getPropertyDict().has_key(JDBC_URL_PROPERTY) and
            properties.getPropertyDict().has_key(JDBC_RCA_URL_PROPERTY)):
    raise FatalException(-1, "Ambari Server not set up yet. Nothing to reset.")

  empty_options = optparse.Values()
  empty_options.must_set_database_options = options.must_set_database_options
  empty_options.database_index = options.database_index
  empty_options.database_host = ""
  empty_options.database_port = ""
  empty_options.database_name = ""
  empty_options.database_windows_auth = False
  empty_options.database_username = ""
  empty_options.database_password = ""
  empty_options.init_db_script_file = ""
  empty_options.cleanup_db_script_file = ""

  factory = DBMSConfigFactory()

  return empty_options, factory, properties

def _reset_database(options):
  properties = get_ambari_properties()
  if properties == -1:
    print_error_msg("Error getting ambari properties")
    return -1

  factory = DBMSConfigFactory()

  dbmsAmbari = factory.create(options, properties)
  dbmsAmbari.reset_database()


#
# Extract the system views
#
def extract_views():
  java_exe_path = get_java_exe_path()
  if java_exe_path is None:
    print_error_msg("No JDK found, please run the \"setup\" "
                    "command to install a JDK automatically or install any "
                    "JDK manually to " + configDefaults.JDK_INSTALL_DIR)
    return 1

  properties = get_ambari_properties()
  if properties == -1:
    print_error_msg("Error getting ambari properties")
    return -1

  vdir = get_value_from_properties(properties, VIEWS_DIR_PROPERTY, configDefaults.DEFAULT_VIEWS_DIR)

  files = [f for f in os.listdir(vdir) if os.path.isfile(os.path.join(vdir,f))]
  for f in files:
    command = VIEW_EXTRACT_CMD.format(java_exe_path,
                                      get_full_ambari_classpath(), os.path.join(vdir,f))
    retcode, stdout, stderr = run_os_command(command)
    if retcode == 0:
      sys.stdout.write(f + "\n")
    elif retcode == 2:
      sys.stdout.write("Error extracting " + f + "\n")
    else:
      sys.stdout.write(".")
      sys.stdout.flush()

    print_info_msg("Return code from extraction of view archive " + f + ": " +
                   str(retcode))

  sys.stdout.write("\n")
  return 0


def unpack_jce_policy():
  properties = get_ambari_properties()
  jdk_path = properties.get_property(JAVA_HOME_PROPERTY)
  jdk_security_path = jdk_path + os.sep + configDefaults.JDK_SECURITY_DIR

  jce_name = properties.get_property(JCE_NAME_PROPERTY)
  jce_zip_path = configDefaults.SERVER_RESOURCES_DIR + os.sep + jce_name
  f = None

  import zipfile
  if os.path.exists(jdk_security_path) and os.path.exists(jce_zip_path):
    try:
      f = zipfile.ZipFile(jce_zip_path, "r")
      zip_members = f.namelist()
      for member in zip_members:
        if member.endswith(os.sep):
          os.makedirs(os.path.join(jdk_security_path, member))
        else:
          f.extract(member, jdk_security_path)
      unziped_jce_path = os.path.split(zip_members[len(zip_members) - 1])[0]
    finally:
      try:
        f.close()
      except Exception as e:
        err = "Fail during the extraction of {0}.".format(jce_zip_path)
        raise FatalException(1, err)
  else:
    err = "The path {0} or {1} is invalid.".format(jdk_security_path, jce_zip_path)
    raise FatalException(1, err)

  if unziped_jce_path:
    from_path = jdk_security_path + os.sep + unziped_jce_path
    jce_files = os.listdir(from_path)
    for i in range(len(jce_files)):
      jce_files[i] = from_path + os.sep + jce_files[i]
    from ambari_commons.os_utils import copy_files
    copy_files(jce_files, jdk_security_path)
    dir_to_delete = jdk_security_path + os.sep + unziped_jce_path.split(os.sep)[0]
    shutil.rmtree(dir_to_delete)
  return 0

#
# Setup the Ambari Server.
#
def setup(options):
  retcode = verify_setup_allowed()
  if not retcode == 0:
    raise FatalException(1, None)

  if not is_root():
    err = configDefaults.MESSAGE_ERROR_SETUP_NOT_ROOT
    raise FatalException(4, err)

  # proceed jdbc properties if they were set
  if _check_jdbc_options(options):
    proceedJDBCProperties(options)
    return

  (retcode, err) = disable_security_enhancements()
  if not retcode == 0:
    raise FatalException(retcode, err)

  #Create ambari user, if needed
  retcode = check_ambari_user()
  if not retcode == 0:
    err = 'Failed to create user. Exiting.'
    raise FatalException(retcode, err)

  print configDefaults.MESSAGE_CHECK_FIREWALL
  check_firewall()

  # proceed jdbc properties if they were set
  if _check_jdbc_options(options):
    proceedJDBCProperties(options)

  print 'Checking JDK...'
  try:
    download_and_install_jdk(options)
  except FatalException as e:
    err = 'Downloading or installing JDK failed: {0}. Exiting.'.format(e)
    raise FatalException(e.code, err)

  if not IS_CUSTOM_JDK: # If it's not a custom JDK, will also install JCE policy automatically
    print 'Installing JCE policy...'
    try:
      unpack_jce_policy()
    except FatalException as e:
      err = 'Installing JCE failed: {0}. Exiting.'.format(e)
      raise FatalException(e.code, err)

  print 'Completing setup...'
  retcode = configure_os_settings()
  if not retcode == 0:
    err = 'Configure of OS settings in ambari.properties failed. Exiting.'
    raise FatalException(retcode, err)

  print 'Configuring database...'
  prompt_db_properties(options)

  #DB setup should be done last after doing any setup.

  _setup_database(options)

  check_jdbc_drivers(options)

  print 'Extracting system views...'
  retcode = extract_views()
  if not retcode == 0:
    err = 'Error while extracting system views. Exiting'
    raise FatalException(retcode, err)

  # we've already done this, but new files were created so run it one time.
  adjust_directory_permissions(read_ambari_user())


#
# Setup the JCE policy for Ambari Server.
#
def setup_jce_policy(args):
  if os.path.exists(args[1]):
    if not os.path.split(args[1])[0] == configDefaults.SERVER_RESOURCES_DIR:
      try:
        shutil.copy(args[1], configDefaults.SERVER_RESOURCES_DIR)
      except Exception as e:
        err = "Fail while trying to copy {0} to {1}. {2}".format(args[1], configDefaults.SERVER_RESOURCES_DIR, e)
        raise FatalException(1, err)
  else:
    err = "Can not run 'setup-jce'. Invalid path {0}.".format(args[1])
    raise FatalException(1, err)

  from ambari_commons.os_utils import search_file
  from ambari_server.serverConfiguration import AMBARI_PROPERTIES_FILE, get_conf_dir
  conf_file = search_file(AMBARI_PROPERTIES_FILE, get_conf_dir())
  properties = get_ambari_properties()
  zip_name = os.path.split(args[1])[1]
  properties.process_pair(JCE_NAME_PROPERTY, zip_name)
  try:
    properties.store(open(conf_file, "w"))
  except Exception, e:
    print_error_msg('Could not write ambari config file "%s": %s' % (conf_file, e))

  print 'Installing JCE policy...'
  try:
    unpack_jce_policy()
  except FatalException as e:
    err = 'Installing JCE failed: {0}. Exiting.'.format(e)
    raise FatalException(e.code, err)
  print 'NOTE: Restart Ambari Server to apply changes' + \
        ' ("ambari-server restart|stop|start")'


#
# Resets the Ambari Server.
#
def reset(options):
  if not is_root():
    err = configDefaults.MESSAGE_ERROR_RESET_NOT_ROOT
    raise FatalException(4, err)

  status, stateDesc = is_server_runing()
  if status:
    err = 'Ambari-server must be stopped to reset'
    raise FatalException(1, err)

  #force reset if silent option provided
  if get_silent():
    default = "yes"
  else:
    default = "no"

  choice = get_YN_input("**** WARNING **** You are about to reset and clear the "
                        "Ambari Server database. This will remove all cluster "
                        "host and configuration information from the database. "
                        "You will be required to re-configure the Ambari server "
                        "and re-run the cluster wizard. \n"
                        "Are you SURE you want to perform the reset "
                        "[yes/no] ({0})? ".format(default), get_silent())
  okToRun = choice
  if not okToRun:
    err = "Ambari Server 'reset' cancelled"
    raise FatalException(1, err)

  _reset_database(options)
  pass
