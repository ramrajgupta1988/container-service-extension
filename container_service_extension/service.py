# container-service-extension
# Copyright (c) 2017 VMware, Inc. All Rights Reserved.
# SPDX-License-Identifier: BSD-2-Clause

from enum import Enum
from enum import unique
import platform
import signal
import sys
import threading
from threading import Thread
import time
import traceback

import click
import pkg_resources
from pyvcloud.vcd.client import ApiVersion as vCDApiVersion
from pyvcloud.vcd.client import BasicLoginCredentials
from pyvcloud.vcd.client import Client
from pyvcloud.vcd.exceptions import EntityNotFoundException
from pyvcloud.vcd.exceptions import OperationNotSupportedException

import container_service_extension.compute_policy_manager \
    as compute_policy_manager
from container_service_extension.config_validator import get_validated_config
from container_service_extension.configure_cse import check_cse_installation
from container_service_extension.consumer import MessageConsumer
import container_service_extension.def_.models as def_models
import container_service_extension.def_.schema_svc as def_schema_svc
import container_service_extension.def_.utils as def_utils
from container_service_extension.def_.utils import raise_error_if_def_not_supported  # noqa: E501
import container_service_extension.exceptions as cse_exception
import container_service_extension.local_template_manager as ltm
import container_service_extension.logger as logger
from container_service_extension.pks_cache import PksCache
import container_service_extension.pyvcloud_utils as vcd_utils
from container_service_extension.server_constants import LocalTemplateKey
from container_service_extension.server_constants import SYSTEM_ORG_NAME
from container_service_extension.shared_constants import ServerAction
from container_service_extension.telemetry.constants import CseOperation
from container_service_extension.telemetry.constants import PayloadKey
from container_service_extension.telemetry.telemetry_handler \
    import record_user_action
from container_service_extension.telemetry.telemetry_handler import \
    record_user_action_details
from container_service_extension.template_rule import TemplateRule
import container_service_extension.utils as utils
from container_service_extension.vsphere_utils import populate_vsphere_list


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(
                *args, **kwargs)
        return cls._instances[cls]


def signal_handler(signal, frame):
    print('\nCrtl+C detected, exiting')
    raise KeyboardInterrupt()


def consumer_thread(c):
    try:
        logger.SERVER_LOGGER.info(f"About to start consumer_thread {c}.")
        c.run()
    except Exception:
        click.echo("About to stop consumer_thread.")
        logger.SERVER_LOGGER.error(traceback.format_exc())
        c.stop()


@unique
class ServerState(Enum):
    RUNNING = 'Running'
    DISABLED = 'Disabled'
    STOPPING = 'Shutting down'
    STOPPED = 'Stopped'


class Service(object, metaclass=Singleton):
    def __init__(self, config_file, pks_config_file=None,
                 should_check_config=True,
                 skip_config_decryption=False, decryption_password=None):
        self.config_file = config_file
        self.pks_config_file = pks_config_file
        self.config = None
        self.should_check_config = should_check_config
        self.skip_config_decryption = skip_config_decryption
        self.decryption_password = decryption_password
        self.consumers = []
        self.threads = []
        self.pks_cache = None
        self._state = ServerState.STOPPED
        self._nativeInterface: def_models.DefInterface = None
        self._nativeEntityType: def_models.DefEntityType = None

    def get_service_config(self):
        return self.config

    def get_pks_cache(self):
        return self.pks_cache

    def is_pks_enabled(self):
        return bool(self.pks_cache)

    def active_requests_count(self):
        n = 0
        # TODO(request_count) Add support for PksBroker - VCDA-938
        for t in threading.enumerate():
            from container_service_extension.vcdbroker import VcdBroker
            if type(t) == VcdBroker:
                n += 1
        return n

    def get_status(self):
        return self._state.value

    def is_running(self):
        return self._state == ServerState.RUNNING

    def info(self, get_sysadmin_info=False):
        result = Service.version()
        if get_sysadmin_info:
            result['consumer_threads'] = len(self.threads)
            result['all_threads'] = threading.activeCount()
            result['requests_in_progress'] = self.active_requests_count()
            result['config_file'] = self.config_file
            result['status'] = self.get_status()
        else:
            del result['python']
        return result

    def get_native_cluster_interface(self) -> def_models.DefInterface:
        return self._nativeInterface

    def get_native_cluster_entity_type(self) -> def_models.DefEntityType:
        return self._nativeEntityType

    @classmethod
    def version(cls):
        return {
            'product': 'CSE',
            'description': 'Container Service Extension for VMware vCloud '
                           'Director',
            'version': pkg_resources.require('container-service-extension')[0].version,  # noqa: E501
            'python': platform.python_version()
        }

    def update_status(self, server_action: ServerAction):
        def graceful_shutdown():
            message = 'Shutting down CSE'
            n = self.active_requests_count()
            if n > 0:
                message += f" CSE will finish processing {n} requests."
            self._state = ServerState.STOPPING
            return message

        if self._state == ServerState.RUNNING:
            if server_action == ServerAction.ENABLE:
                return 'CSE is already enabled and running.'
            if server_action == ServerAction.DISABLE:
                self._state = ServerState.DISABLED
                return 'CSE has been disabled.'
            if server_action == ServerAction.STOP:
                raise cse_exception.BadRequestError(
                    error_message='CSE must be disabled before '
                                  'it can be stopped.')
            raise cse_exception.BadRequestError(
                error_message=f"Invalid server action: '{server_action}'")
        if self._state == ServerState.DISABLED:
            if server_action == ServerAction.ENABLE:
                self._state = ServerState.RUNNING
                return 'CSE has been enabled and is running.'
            if server_action == ServerAction.DISABLE:
                return 'CSE is already disabled.'
            if server_action == ServerAction.STOP:
                return graceful_shutdown()
        if self._state == ServerState.STOPPING:
            if server_action == ServerAction.ENABLE:
                raise cse_exception.BadRequestError(
                    error_message='Cannot enable CSE while it is being'
                                  'stopped.')
            if server_action == ServerAction.DISABLE:
                raise cse_exception.BadRequestError(
                    error_message='Cannot disable CSE while it is being'
                                  ' stopped.')
            if server_action == ServerAction.STOP:
                return graceful_shutdown()

        raise cse_exception.CseServerError(f"Invalid server state: '{self._state}'")  # noqa: E501

    def run(self, msg_update_callback=utils.NullPrinter()):
        self.config = get_validated_config(
            self.config_file,
            pks_config_file_name=self.pks_config_file,
            skip_config_decryption=self.skip_config_decryption,
            decryption_password=self.decryption_password,
            log_wire_file=logger.SERVER_DEBUG_WIRELOG_FILEPATH,
            logger_debug=logger.SERVER_LOGGER,
            msg_update_callback=msg_update_callback)

        populate_vsphere_list(self.config['vcs'])

        # Load def entity-type and interface
        self._load_def_schema(msg_update_callback=msg_update_callback)

        # Read k8s catalog definition from catalog item metadata and append
        # the same to to server run-time config
        self._load_template_definition_from_catalog(
            msg_update_callback=msg_update_callback)

        if float(self.config['vcd']['api_version']) < float(vCDApiVersion.VERSION_35.value): # noqa: E501
            # Read templates rules from config and update template deinfition
            # in server run-time config
            self._process_template_rules(
                msg_update_callback=msg_update_callback)

            # Make sure that all vms in templates are compliant with the
            # compute policy specified in template definition (can be affected
            # by rules).
            self._process_template_compute_policy_compliance(
                msg_update_callback=msg_update_callback)
        else:
            msg = "Template rules are not supported by CSE for vCD api " \
                  "version 35.0 or above. Skipping template rule processing."
            msg_update_callback.info(msg)
            logger.SERVER_LOGGER.debug(msg)

        if self.should_check_config:
            check_cse_installation(
                self.config, msg_update_callback=msg_update_callback)

        if self.config.get('pks_config'):
            pks_config = self.config.get('pks_config')
            self.pks_cache = PksCache(
                pks_servers=pks_config.get('pks_api_servers', []),
                pks_accounts=pks_config.get('pks_accounts', []),
                pvdcs=pks_config.get('pvdcs', []),
                orgs=pks_config.get('orgs', []),
                nsxt_servers=pks_config.get('nsxt_servers', []))

        amqp = self.config['amqp']
        num_consumers = self.config['service']['listeners']
        for n in range(num_consumers):
            try:
                c = MessageConsumer(
                    amqp['host'], amqp['port'], amqp['ssl'], amqp['vhost'],
                    amqp['username'], amqp['password'], amqp['exchange'],
                    amqp['routing_key'])
                name = 'MessageConsumer-%s' % n
                t = Thread(name=name, target=consumer_thread, args=(c, ))
                t.daemon = True
                t.start()
                msg = f"Started thread '{name} ({t.ident})'"
                msg_update_callback.general(msg)
                logger.SERVER_LOGGER.info(msg)
                self.threads.append(t)
                self.consumers.append(c)
                time.sleep(0.25)
            except KeyboardInterrupt:
                break
            except Exception:
                logger.SERVER_LOGGER.error(traceback.format_exc())

        logger.SERVER_LOGGER.info(f"Number of threads started: {len(self.threads)}")  # noqa: E501

        self._state = ServerState.RUNNING

        message = f"Container Service Extension for vCloud Director" \
                  f"\nServer running using config file: {self.config_file}" \
                  f"\nLog files: {logger.SERVER_INFO_LOG_FILEPATH}, " \
                  f"{logger.SERVER_DEBUG_LOG_FILEPATH}" \
                  f"\nwaiting for requests (ctrl+c to close)"

        signal.signal(signal.SIGINT, signal_handler)
        msg_update_callback.general_no_color(message)
        logger.SERVER_LOGGER.info(message)

        # Record telemetry on user action and details of operation.
        cse_params = {
            PayloadKey.WAS_DECRYPTION_SKIPPED: bool(self.skip_config_decryption), # noqa: E501
            PayloadKey.WAS_PKS_CONFIG_FILE_PROVIDED: bool(self.pks_config_file),  # noqa: E501
            PayloadKey.WAS_INSTALLATION_CHECK_SKIPPED: bool(self.should_check_config)  # noqa: E501
        }
        record_user_action_details(cse_operation=CseOperation.SERVICE_RUN,
                                   cse_params=cse_params)
        record_user_action(cse_operation=CseOperation.SERVICE_RUN)

        while True:
            try:
                time.sleep(1)
                if self._state == ServerState.STOPPING and \
                        self.active_requests_count() == 0:
                    break
            except KeyboardInterrupt:
                break
            except Exception:
                msg_update_callback.general_no_color(
                    traceback.format_exc())
                logger.SERVER_LOGGER.error(traceback.format_exc())
                sys.exit(1)

        logger.SERVER_LOGGER.info("Stop detected")
        logger.SERVER_LOGGER.info("Closing connections...")
        for c in self.consumers:
            try:
                c.stop()
            except Exception:
                logger.SERVER_LOGGER.error(traceback.format_exc())

        self._state = ServerState.STOPPED
        logger.SERVER_LOGGER.info("Done")

    def _load_def_schema(self, msg_update_callback=utils.NullPrinter()):
        """Load cluster interface and cluster entity type to global context.

        If defined entity framework is supported by vCD api version, load
        defined entity interface and defined entity type registered during
        server install

        :param utils.ConsoleMessagePrinter msg_update_callback
        """
        sysadmin_client = None
        try:
            sysadmin_client = vcd_utils.get_sys_admin_client()
            logger_wire = logger.NULL_LOGGER
            if utils.str_to_bool(utils.str_to_bool(self.config['service'].get('log_wire', False))): # noqa: E501
                logger_wire = logger.SERVER_CLOUDAPI_WIRE_LOGGER

            cloudapi_client = \
                vcd_utils.get_cloudapi_client_from_vcd_client(sysadmin_client,
                                                              logger.SERVER_LOGGER, # noqa: E501
                                                              logger_wire)
            raise_error_if_def_not_supported(cloudapi_client)
            schema_svc = def_schema_svc.DefSchemaService(cloudapi_client)
            defKey = def_utils.DefKey
            keys_map = def_utils.MAP_API_VERSION_TO_KEYS[float(sysadmin_client.get_api_version())] # noqa: E501
            interface_id = def_utils.generate_interface_id(vendor=keys_map[defKey.VENDOR], # noqa: E501
                                                           nss=keys_map[defKey.INTERFACE_NSS], # noqa: E501
                                                           version=keys_map[defKey.INTERFACE_VERSION]) # noqa: E501
            entity_type_id = def_utils.generate_entity_type_id(vendor=keys_map[defKey.VENDOR], # noqa: E501
                                                               nss=keys_map[defKey.ENTITY_TYPE_NSS], # noqa: E501
                                                               version=keys_map[defKey.ENTITY_TYPE_VERSION]) # noqa: E501
            self._nativeInterface = schema_svc.get_interface(interface_id)
            self._nativeEntityType = schema_svc.get_entity_type(entity_type_id)
            msg = "Successfully loaded defined entity schema to global context"
            msg_update_callback.general(msg)
            logger.SERVER_LOGGER.debug(msg)
        except cse_exception.DefNotSupportedException:
            msg = "Skipping initialization of defined entity type" \
                  " and defined entity interface"
            msg_update_callback.info(msg)
            logger.SERVER_LOGGER.debug(msg)
        except Exception as e:
            msg = f"Failed to load defined entity schema to global context: {str(e)}" # noqa: E501
            msg_update_callback.error(e)
            logger.SERVER_LOGGER.error(e)
            raise(e)
        finally:
            if sysadmin_client:
                sysadmin_client.logout()

    def _load_template_definition_from_catalog(self,
                                               msg_update_callback=utils.NullPrinter()): # noqa: E501
        msg = "Loading k8s template definition from catalog"
        logger.SERVER_LOGGER.info(msg)
        msg_update_callback.general_no_color(msg)

        client = None
        try:
            log_filename = None
            log_wire = \
                utils.str_to_bool(self.config['service'].get('log_wire'))
            if log_wire:
                log_filename = logger.SERVER_DEBUG_WIRELOG_FILEPATH

            client = Client(self.config['vcd']['host'],
                            api_version=self.config['vcd']['api_version'],
                            verify_ssl_certs=self.config['vcd']['verify'],
                            log_file=log_filename,
                            log_requests=log_wire,
                            log_headers=log_wire,
                            log_bodies=log_wire)
            credentials = BasicLoginCredentials(self.config['vcd']['username'],
                                                SYSTEM_ORG_NAME,
                                                self.config['vcd']['password'])
            client.set_credentials(credentials)

            org_name = self.config['broker']['org']
            catalog_name = self.config['broker']['catalog']
            k8_templates = ltm.get_all_k8s_local_template_definition(
                client=client, catalog_name=catalog_name, org_name=org_name)

            if not k8_templates:
                msg = "No valid K8 templates were found in catalog " \
                      f"'{catalog_name}'. Unable to start CSE server."
                msg_update_callback.error(msg)
                logger.SERVER_LOGGER.error(msg)
                sys.exit(1)

            # Check that default k8s template exists in vCD at the correct
            # revision
            default_template_name = \
                self.config['broker']['default_template_name']
            default_template_revision = \
                str(self.config['broker']['default_template_revision'])
            found_default_template = False
            for template in k8_templates:
                if str(template[LocalTemplateKey.REVISION]) == default_template_revision and template[LocalTemplateKey.NAME] == default_template_name: # noqa: E501
                    found_default_template = True

                msg = f"Found K8 template '{template['name']}' at revision " \
                      f"{template['revision']} in catalog '{catalog_name}'"
                msg_update_callback.general(msg)
                logger.SERVER_LOGGER.info(msg)

            if not found_default_template:
                msg = f"Default template {default_template_name} with " \
                      f"revision {default_template_revision} not found." \
                      " Unable to start CSE server."
                msg_update_callback.error(msg)
                logger.SERVER_LOGGER.error(msg)
                sys.exit(1)

            self.config['broker']['templates'] = k8_templates
        finally:
            if client:
                client.logout()

    def _process_template_rules(self, msg_update_callback=utils.NullPrinter()):
        if 'template_rules' not in self.config:
            return
        rules = self.config['template_rules']
        if not rules:
            return

        templates = self.config['broker']['templates']

        # process rules
        msg = "Processing template rules."
        logger.SERVER_LOGGER.debug(msg)
        msg_update_callback.general_no_color(msg)

        for rule_def in rules:
            rule = TemplateRule(
                name=rule_def.get('name'), target=rule_def.get('target'),
                action=rule_def.get('action'), logger=logger.SERVER_LOGGER,
                msg_update_callback=msg_update_callback)

            msg = f"Processing rule : {rule}."
            logger.SERVER_LOGGER.debug(msg)
            msg_update_callback.general_no_color(msg)

            # Since the patching is in-place, the changes will reflect back on
            # the dictionary holding the server runtime configuration.
            rule.apply(templates)

            msg = f"Finished processing rule : '{rule.name}'"
            logger.SERVER_LOGGER.debug(msg)
            msg_update_callback.general(msg)

    def _process_template_compute_policy_compliance(self,
                                                    msg_update_callback=utils.NullPrinter()): # noqa: E501
        msg = "Processing compute policy for k8s templates."
        logger.SERVER_LOGGER.info(msg)
        msg_update_callback.general_no_color(msg)

        org_name = self.config['broker']['org']
        catalog_name = self.config['broker']['catalog']
        sysadmin_client = None
        try:
            sysadmin_client = vcd_utils.get_sys_admin_client()
            cpm = compute_policy_manager.ComputePolicyManager(sysadmin_client,
                                                              log_wire=self.config['service'].get('log_wire')) # noqa: E501

            for template in self.config['broker']['templates']:
                policy_name = template[LocalTemplateKey.COMPUTE_POLICY]
                catalog_item_name = template[LocalTemplateKey.CATALOG_ITEM_NAME] # noqa: E501
                # if policy name is not empty, stamp it on the template
                if policy_name:
                    try:
                        policy = cpm.get_vdc_compute_policy(policy_name=policy_name) # noqa: E501
                    except EntityNotFoundException:
                        # create the policy if it does not exist
                        msg = f"Creating missing compute policy " \
                              f"'{policy_name}'."
                        msg_update_callback.info(msg)
                        logger.SERVER_LOGGER.debug(msg)
                        policy = cpm.add_vdc_compute_policy(policy_name=policy_name) # noqa: E501

                    msg = f"Assigning compute policy '{policy_name}' to " \
                          f"template '{catalog_item_name}'."
                    msg_update_callback.general(msg)
                    logger.SERVER_LOGGER.debug(msg)
                    cpm.assign_vdc_sizing_policy_to_vapp_template_vms(
                        compute_policy_href=policy['href'],
                        org_name=org_name,
                        catalog_name=catalog_name,
                        catalog_item_name=catalog_item_name)
                else:
                    # empty policy name means we should remove policy from
                    # template
                    msg = f"Removing compute policy from template " \
                          f"'{catalog_item_name}'."
                    msg_update_callback.general(msg)
                    logger.SERVER_LOGGER.debug(msg)

                    cpm.remove_all_vdc_compute_policies_from_vapp_template_vms(
                        org_name=org_name,
                        catalog_name=catalog_name,
                        catalog_item_name=catalog_item_name)
        except OperationNotSupportedException:
            msg = "Compute policy not supported by vCD. Skipping " \
                  "assigning/removing it to/from templates."
            msg_update_callback.info(msg)
            logger.SERVER_LOGGER.debug(msg)
        finally:
            if sysadmin_client is not None:
                sysadmin_client.logout()
