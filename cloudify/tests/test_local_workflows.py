########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.

import contextlib
import time
import yaml
import sys
import tempfile
import unittest
import shutil
import os
import threading
import Queue

import nose.tools

import cloudify.logs
from cloudify.decorators import workflow, operation

from cloudify.workflows import local
from cloudify.workflows import workflow_context
from cloudify.workflows.workflow_context import task_config


@nose.tools.nottest
class BaseWorkflowTest(unittest.TestCase):

    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix='cloudify-workflows-')
        self.storage_dir = os.path.join(self.work_dir, 'storage')
        self.storage_kwargs = {}
        self.env = None
        os.mkdir(self.storage_dir)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        shutil.rmtree(self.work_dir)

    def _load_env(self, blueprint_path, inputs=None, name=None):
        if name is None:
            name = self._testMethodName
        return local.Environment(blueprint_path,
                                 name=name,
                                 inputs=inputs,
                                 storage_cls=self.storage_cls,
                                 **self.storage_kwargs)

    def _execute_workflow(self,
                          workflow_method=None,
                          operation_methods=None,
                          use_existing_env=True,
                          execute_kwargs=None,
                          name=None,
                          inputs=None,
                          create_blueprint_func=None,
                          workflow_parameters_schema=None,
                          workflow_name='workflow'):
        if create_blueprint_func is None:
            create_blueprint_func = self._blueprint_1

        execute_kwargs = execute_kwargs or {}

        def stub_op(ctx, **_):
            pass
        if operation_methods is None:
            operation_methods = [stub_op]

        if workflow_method is None and len(operation_methods) == 1:
            def workflow_method(ctx, **_):
                instance = _instance(ctx, 'node')
                instance.set_state('state').get()
                instance.execute_operation('test.op0')

        # same as @workflow above the method
        workflow_method = workflow(workflow_method, force_not_celery=True)

        # same as @operation above each op method
        operation_methods = [operation(m, force_not_celery=True)
                             for m in operation_methods]

        temp_module = self._create_temp_module()

        setattr(temp_module,
                workflow_method.__name__,
                workflow_method)
        for operation_method in operation_methods:
            setattr(temp_module,
                    operation_method.__name__,
                    operation_method)

        blueprint = create_blueprint_func(workflow_method,
                                          operation_methods,
                                          workflow_parameters_schema)
        try:
            blueprint_dir = os.path.join(self.work_dir, 'blueprint')
            if not os.path.isdir(blueprint_dir):
                os.mkdir(blueprint_dir)
            with open(os.path.join(blueprint_dir, 'resource'), 'w') as f:
                f.write('content')
            blueprint_path = os.path.join(blueprint_dir, 'blueprint.yaml')
            with open(blueprint_path, 'w') as f:
                f.write(yaml.safe_dump(blueprint))
            if not self.env or not use_existing_env:
                self.env = self._load_env(blueprint_path,
                                          inputs=inputs,
                                          name=name)

            final_execute_kwargs = {
                'task_retries': 0,
                'task_retry_interval': 1
            }
            final_execute_kwargs.update(execute_kwargs)

            self.env.execute(workflow_name, **final_execute_kwargs)
        finally:
            self._remove_temp_module()

    def _blueprint_1(self, workflow_method, operation_methods,
                     workflow_parameters_schema):
        interfaces = {
            'test': [
                {'op{}'.format(index):
                 'p.{}.{}'.format(self._testMethodName,
                                  op_method.__name__)}
                for index, op_method in
                enumerate(operation_methods)
            ]
        }

        blueprint = {
            'inputs': {
                'from_input': {
                    'default': 'from_input_default_value'
                }
            },
            'outputs': {
                'some_output': {
                    'value': {'get_attribute': ['node', 'some_output']}
                }
            },
            'plugins': {
                'p': {
                    'derived_from': 'cloudify.plugins.manager_plugin'
                }
            },
            'node_types': {
                'type': {
                    'properties': {
                        'property': {
                            'default': 'default'
                        },
                        'from_input': {
                            'default': 'from_input_default_value'
                        }
                    }
                }
            },
            'relationships': {
                'cloudify.relationships.contained_in': {}
            },
            'node_templates': {
                'node2': {
                    'type': 'type',
                    'interfaces': interfaces,
                },
                'node': {
                    'type': 'type',
                    'interfaces': interfaces,
                    'properties': {
                        'property': 'value',
                        'from_input': {'get_input': 'from_input'}
                    },
                    'relationships': [{
                        'target': 'node2',
                        'type': 'cloudify.relationships.contained_in',
                        'source_interfaces': interfaces,
                        'target_interfaces': interfaces
                    }]
                },
            },
            'workflows': {
                'workflow': {
                    'mapping': 'p.{}.{}'.format(self._testMethodName,
                                                workflow_method.__name__),
                    'parameters': workflow_parameters_schema or {}
                }
            }
        }
        return blueprint

    def _create_temp_module(self):
        import imp
        temp_module = imp.new_module(self._testMethodName)
        sys.modules[self._testMethodName] = temp_module
        return temp_module

    def _remove_temp_module(self):
        del sys.modules[self._testMethodName]

    @contextlib.contextmanager
    def _mock_stdout_event_and_log(self):
        events = []
        logs = []

        def mock_stdout_event(event):
            events.append(event)

        def mock_stdout_log(log):
            logs.append(log)

        o_stdout_event = cloudify.logs.stdout_event_out
        o_stdout_log = cloudify.logs.stdout_log_out
        cloudify.logs.stdout_event_out = mock_stdout_event
        cloudify.logs.stdout_log_out = mock_stdout_log

        try:
            yield events, logs
        finally:
            cloudify.logs.stdout_event_out = o_stdout_log
            cloudify.logs.stdout_event_out = o_stdout_event


@nose.tools.nottest
class LocalWorkflowTest(BaseWorkflowTest):

    def test_workflow_and_operation_logging_and_events(self):

        def assert_task_events(indexes, events):
            self.assertEqual('sending_task',
                             events[indexes[0]]['event_type'])
            self.assertEqual('task_started',
                             events[indexes[1]]['event_type'])
            self.assertEqual('task_succeeded',
                             events[indexes[2]]['event_type'])

        def the_workflow(ctx, **_):
            def local_task():
                pass
            instance = _instance(ctx, 'node')
            ctx.logger.info('workflow_logging')
            ctx.send_event('workflow_event').get()
            instance.logger.info('node_instance_logging')
            instance.send_event('node_instance_event').get()
            instance.execute_operation('test.op0').get()
            ctx.local_task(local_task).get()

        def the_operation(ctx, **_):
            ctx.logger.info('op_logging')
            ctx.send_event('op_event')

        with self._mock_stdout_event_and_log() as (events, logs):
            self._execute_workflow(the_workflow, operation_methods=[
                the_operation])

            self.assertEqual(11, len(events))
            self.assertEqual(3, len(logs))
            self.assertEqual('workflow_started',
                             events[0]['event_type'])
            self.assertEqual('workflow_event',
                             events[1]['message']['text'])
            self.assertEqual('node_instance_event',
                             events[2]['message']['text'])
            assert_task_events([3, 4, 6], events)
            self.assertEqual('op_event',
                             events[5]['message']['text'])
            assert_task_events([7, 8, 9], events)
            self.assertEqual('workflow_succeeded',
                             events[10]['event_type'])
            self.assertEqual('workflow_logging',
                             logs[0]['message']['text'])
            self.assertEqual('node_instance_logging',
                             logs[1]['message']['text'])
            self.assertEqual('op_logging',
                             logs[2]['message']['text'])

    def test_task_event_filtering(self):

        def flow1(ctx, **_):
            def task():
                pass
            ctx.local_task(task)

        with self._mock_stdout_event_and_log() as (events, _):
            self._execute_workflow(flow1, use_existing_env=False)
            self.assertEqual(5, len(events))

        def flow2(ctx, **_):
            def task():
                pass
            ctx.local_task(task, send_task_events=False)

        with self._mock_stdout_event_and_log() as (events, _):
            self._execute_workflow(flow2, use_existing_env=False)
            self.assertEqual(2, len(events))

        def flow3(ctx, **_):
            @task_config(send_task_events=False)
            def task():
                pass
            ctx.local_task(task)

        with self._mock_stdout_event_and_log() as (events, _):
            self._execute_workflow(flow3, use_existing_env=False)
            self.assertEqual(2, len(events))

        def flow4(ctx, **_):
            @task_config(send_task_events=True)
            def task():
                pass
            ctx.local_task(task)

        with self._mock_stdout_event_and_log() as (events, _):
            self._execute_workflow(flow4, use_existing_env=False)
            self.assertEqual(5, len(events))

        def flow5(ctx, **_):
            def task():
                self.fail()
            ctx.local_task(task, send_task_events=False)

        with self._mock_stdout_event_and_log() as (events, _):
            self.assertRaises(AssertionError,
                              self._execute_workflow,
                              flow5, use_existing_env=False)
            self.assertEqual(3, len(events))
            self.assertEqual('task_failed', events[1]['event_type'])
            self.assertEqual('workflow_failed', events[2]['event_type'])

    def test_task_config_decorator(self):
        def flow(ctx, **_):
            task_config_kwargs = {'key': 'task_config'}
            invocation_kwargs = {'key': 'invocation'}

            @task_config(kwargs=task_config_kwargs)
            def task1(**kwargs):
                self.assertDictEqual(kwargs, task_config_kwargs)
            ctx.local_task(task1).get()

            @task_config(kwargs=task_config_kwargs)
            def task2(**kwargs):
                self.assertDictEqual(kwargs, task_config_kwargs)
            ctx.local_task(task2, kwargs=invocation_kwargs).get()

            @task_config(kwargs=task_config_kwargs)
            def task2(**kwargs):
                self.assertDictEqual(kwargs, invocation_kwargs)
            ctx.local_task(task2,
                           kwargs=invocation_kwargs,
                           override_task_config=True).get()
        self._execute_workflow(flow)

    def test_workflow_bootstrap_context(self):
        def bootstrap_context(ctx, **_):
            bootstrap_context = ctx.internal._get_bootstrap_context()
            self.assertDictEqual(bootstrap_context, {})
        self._execute_workflow(bootstrap_context)

    def test_update_execution_status(self):
        def update_execution_status(ctx, **_):
            ctx.update_execution_status('status')
        self.assertRaises(NotImplementedError,
                          self._execute_workflow,
                          update_execution_status)

    def test_workflow_set_get_node_instance_state(self):
        def get_set_node_instance_state(ctx, **_):
            instance = _instance(ctx, 'node')
            self.assertIsNone(instance.get_state().get())
            instance.set_state('state').get()
            self.assertEquals('state', instance.get_state().get())
        self._execute_workflow(get_set_node_instance_state)

    def test_workflow_ctx_properties(self):
        def attributes(ctx, **_):
            self.assertEqual(self._testMethodName, ctx.blueprint_id)
            self.assertEqual(self._testMethodName, ctx.deployment_id)
            self.assertEqual('workflow', ctx.workflow_id)
            self.assertIsNotNone(ctx.execution_id)
        self._execute_workflow(attributes)

    def test_workflow_blueprint_model(self):
        def blueprint_model(ctx, **_):
            nodes = list(ctx.nodes)
            node1 = ctx.get_node('node')
            node2 = ctx.get_node('node2')
            node1_instances = list(node1.instances)
            node2_instances = list(node2.instances)
            instance1 = node1_instances[0]
            instance2 = node2_instances[0]
            node1_relationships = list(node1.relationships)
            node2_relationships = list(node2.relationships)
            instance1_relationships = list(instance1.relationships)
            instance2_relationships = list(instance2.relationships)
            relationship = node1_relationships[0]
            relationship_instance = instance1_relationships[0]

            self.assertEqual(2, len(nodes))
            self.assertEqual(1, len(node1_instances))
            self.assertEqual(1, len(node2_instances))
            self.assertEqual(1, len(node1_relationships))
            self.assertEqual(0, len(node2_relationships))
            self.assertEqual(1, len(instance1_relationships))
            self.assertEqual(0, len(instance2_relationships))

            sorted_ops = ['op0', 'test.op0']

            self.assertEqual('node', node1.id)
            self.assertEqual('node2', node2.id)
            self.assertEqual('type', node1.type)
            self.assertEqual('type', node1.type)
            self.assertEqual('type', node2.type)
            self.assertListEqual(['type'], node1.type_hierarchy)
            self.assertListEqual(['type'], node2.type_hierarchy)
            self.assertDictContainsSubset({'property': 'value'},
                                          node1.properties)
            self.assertDictContainsSubset({'property': 'default'},
                                          node2.properties)
            self.assertListEqual(sorted_ops, sorted(node1.operations.keys()))
            self.assertListEqual(sorted_ops, sorted(node2.operations.keys()))
            self.assertIs(relationship, node1.get_relationship('node2'))

            self.assertIn('node_', instance1.id)
            self.assertIn('node2_', instance2.id)
            self.assertEqual('node', instance1.node_id)
            self.assertEqual('node2', instance2.node_id)
            self.assertIs(node1, instance1.node)
            self.assertIs(node2, instance2.node)

            self.assertEqual(node2.id, relationship.target_id)
            self.assertEqual(node2, relationship.target_node)
            self.assertListEqual(sorted_ops,
                                 sorted(relationship.source_operations.keys()))
            self.assertListEqual(sorted_ops,
                                 sorted(relationship.target_operations.keys()))

            self.assertEqual(instance2.id, relationship_instance.target_id)
            self.assertEqual(instance2,
                             relationship_instance.target_node_instance)
            self.assertIs(relationship, relationship_instance.relationship)

        self._execute_workflow(blueprint_model)

    def test_operation_capabilities(self):
        def the_workflow(ctx, **_):
            instance = _instance(ctx, 'node')
            instance2 = _instance(ctx, 'node2')
            instance2.execute_operation('test.op0').get()
            instance.execute_operation('test.op1').get()

        def op0(ctx, **_):
            ctx.runtime_properties['key'] = 'value'

        def op1(ctx, **_):
            caps = ctx.capabilities.get_all()
            self.assertEqual(1, len(caps))
            key, value = next(caps.iteritems())
            self.assertIn('node2_', key)
            self.assertDictEqual(value, {'key': 'value'})

        self._execute_workflow(the_workflow, operation_methods=[op0, op1])

    def test_operation_runtime_properties(self):
        def runtime_properties(ctx, **_):
            instance = _instance(ctx, 'node')
            instance.execute_operation('test.op0').get()
            instance.execute_operation('test.op1').get()

        def op0(ctx, **_):
            ctx.runtime_properties['key'] = 'value'

        def op1(ctx, **_):
            self.assertEqual('value', ctx.runtime_properties['key'])

        self._execute_workflow(runtime_properties, operation_methods=[
            op0, op1])

    def test_operation_related_properties(self):
        def the_workflow(ctx, **_):
            instance = _instance(ctx, 'node')
            relationship = next(instance.relationships)
            relationship.execute_source_operation('test.op0')
            relationship.execute_target_operation('test.op0')

        def op(ctx, **_):
            if 'node2_' in ctx.related.node_id:
                self.assertDictContainsSubset({'property': 'default'},
                                              ctx.related.properties)
            elif 'node_' in ctx.related.node_id:
                self.assertDictContainsSubset({'property': 'value'},
                                              ctx.related.properties)
            else:
                self.fail('unexpected: {}'.format(ctx.related.node_id))

        self._execute_workflow(the_workflow, operation_methods=[op])

    def test_operation_related_runtime_properties(self):
        def related_runtime_properties(ctx, **_):
            instance = _instance(ctx, 'node')
            instance2 = _instance(ctx, 'node2')
            relationship = next(instance.relationships)
            instance.execute_operation('test.op0',
                                       kwargs={'value': 'instance1'}).get()
            instance2.execute_operation('test.op0',
                                        kwargs={'value': 'instance2'}).get()
            relationship.execute_source_operation(
                'test.op1', kwargs={'value': 'instance2'}).get()
            relationship.execute_target_operation(
                'test.op1', kwargs={'value': 'instance1'}).get()

        def op0(ctx, value, **_):
            ctx.runtime_properties['key'] = value

        def op1(ctx, value, **_):
            self.assertEqual(value, ctx.related.runtime_properties['key'])

        self._execute_workflow(related_runtime_properties, operation_methods=[
            op0, op1])

    def test_operation_ctx_properties_and_methods(self):
        def ctx_properties(ctx, **_):
            self.assertEqual('node', ctx.node_name)
            self.assertIn('node_', ctx.node_id)
            self.assertEqual('state', ctx.node_state)
            self.assertEqual(self._testMethodName, ctx.blueprint_id)
            self.assertEqual(self._testMethodName, ctx.deployment_id)
            self.assertIsNotNone(ctx.execution_id)
            self.assertEqual('workflow', ctx.workflow_id)
            self.assertIsNotNone(ctx.task_id)
            self.assertEqual('{}.{}'.format(self._testMethodName,
                                            'ctx_properties'),
                             ctx.task_name)
            self.assertIsNone(ctx.task_target)
            self.assertEqual('127.0.0.1', ctx.host_ip)
            self.assertEqual('127.0.0.1', ctx.host_ip)
            self.assertEqual('p', ctx.plugin)
            self.assertEqual('test.op0', ctx.operation)
            self.assertDictContainsSubset({'property': 'value'},
                                          ctx.properties)
            self.assertEqual('content', ctx.get_resource('resource'))
            target_path = ctx.download_resource('resource')
            with open(target_path) as f:
                self.assertEqual('content', f.read())
            expected_target_path = os.path.join(self.work_dir, 'resource')
            target_path = ctx.download_resource(
                'resource', target_path=expected_target_path)
            self.assertEqual(target_path, expected_target_path)
            with open(target_path) as f:
                self.assertEqual('content', f.read())
        self._execute_workflow(operation_methods=[ctx_properties])

    def test_operation_bootstrap_context(self):
        def contexts(ctx, **_):
            self.assertDictEqual({}, ctx.bootstrap_context._bootstrap_context)
            self.assertDictEqual({}, ctx.provider_context)
        self._execute_workflow(operation_methods=[contexts])

    def test_workflow_graph_mode(self):
        def flow(ctx, **_):
            instance = _instance(ctx, 'node')
            graph = ctx.graph_mode()
            sequence = graph.sequence()
            sequence.add(instance.execute_operation('test.op2'))
            sequence.add(instance.execute_operation('test.op1'))
            sequence.add(instance.execute_operation('test.op0'))
            graph.execute()

        def op0(ctx, **_):
            invocation = ctx.runtime_properties['invocation']
            self.assertEqual(2, invocation)

        def op1(ctx, **_):
            invocation = ctx.runtime_properties['invocation']
            self.assertEqual(1, invocation)
            ctx.runtime_properties['invocation'] += 1

        def op2(ctx, **_):
            invocation = ctx.runtime_properties.get('invocation')
            self.assertIsNone(invocation)
            ctx.runtime_properties['invocation'] = 1

        self._execute_workflow(flow, operation_methods=[op0, op1, op2])

    def test_node_instance_version_conflict(self):
        def flow(ctx, **_):
            pass
        # stub to get a properly initialized storage instance
        self._execute_workflow(flow)
        storage = self.env.storage
        instance = storage.get_node_instances()[0]
        storage.update_node_instance(
            instance.id,
            runtime_properties={},
            state=instance.state,
            version=instance.version)
        instance_id = instance.id
        exception = Queue.Queue()
        done = Queue.Queue()

        def proceed():
            try:
                done.get_nowait()
                return False
            except Queue.Empty:
                return True

        def publisher(key, value):
            def func():
                timeout = time.time() + 5
                while time.time() < timeout and proceed():
                    p_instance = storage.get_node_instance(instance_id)
                    p_instance.runtime_properties[key] = value
                    try:
                        storage.update_node_instance(
                            p_instance.id,
                            runtime_properties=p_instance.runtime_properties,
                            state=p_instance.state,
                            version=p_instance.version)
                    except local.StorageConflictError, e:
                        exception.put(e)
                        done.put(True)
                        return
            return func

        publisher1 = publisher('publisher1', 'value1')
        publisher2 = publisher('publisher2', 'value2')

        publisher1_thread = threading.Thread(target=publisher1)
        publisher2_thread = threading.Thread(target=publisher2)

        publisher1_thread.daemon = True
        publisher2_thread.daemon = True

        publisher1_thread.start()
        publisher2_thread.start()

        publisher1_thread.join()
        publisher2_thread.join()

        conflict_error = exception.get_nowait()

        self.assertIn('does not match current', conflict_error.message)


@nose.tools.istest
class LocalWorkflowTestInMemoryStorage(LocalWorkflowTest):

    def setUp(self):
        super(LocalWorkflowTestInMemoryStorage, self).setUp()
        self.storage_cls = local.InMemoryStorage


@nose.tools.istest
class LocalWorkflowTestFileStorage(LocalWorkflowTest):

    def setUp(self):
        super(LocalWorkflowTestFileStorage, self).setUp()
        self.storage_cls = local.FileStorage
        self.storage_kwargs = {'storage_dir': self.storage_dir}


@nose.tools.istest
class FileStorageTest(BaseWorkflowTest):

    def setUp(self):
        super(FileStorageTest, self).setUp()
        self.storage_cls = local.FileStorage
        self.storage_kwargs = {'storage_dir': self.storage_dir}

    def test_storage_dir(self):
        def stub_workflow(ctx, **_):
            pass
        self._execute_workflow(stub_workflow, name=self._testMethodName)
        self.assertTrue(os.path.isdir(
            os.path.join(self.storage_dir, self._testMethodName)))

    def test_persistency(self):
        self._test_persistency(clear=False)

    def test_clear(self):
        self._test_persistency(clear=True)

    def _test_persistency(self, clear):
        def persistency_1(ctx, **_):
            instance = _instance(ctx, 'node')
            instance.set_state('persistency')

        def persistency_2(ctx, **_):
            expected = None if clear else 'persistency'
            instance = _instance(ctx, 'node')
            self.assertEqual(expected, instance.get_state().get())

        self._execute_workflow(persistency_1)
        self.storage_kwargs.update({'clear': clear})
        self._execute_workflow(persistency_2, use_existing_env=False)


@nose.tools.istest
class LocalWorkflowEnvironmentTest(BaseWorkflowTest):

    def setUp(self):
        super(LocalWorkflowEnvironmentTest, self).setUp()
        self.storage_cls = local.InMemoryStorage

    def test_inputs(self):
        def op(ctx, **_):
            self.assertEqual('new_input', ctx.properties['from_input'])
        self._execute_workflow(operation_methods=[op],
                               inputs={'from_input': 'new_input'})

    def test_outputs(self):
        def op(ctx, **_):
            pass
        self._execute_workflow(operation_methods=[op],
                               use_existing_env=False)
        self.assertEqual(self.env.outputs(),
                         {'some_output': {'value': [None]}})

        def op(ctx, **_):
            ctx.runtime_properties['some_output'] = 'value'
        self._execute_workflow(operation_methods=[op],
                               use_existing_env=False)
        self.assertEqual(self.env.outputs(),
                         {'some_output': {'value': ['value']}})

    def test_workflow_parameters(self):
        normal_schema = {
            'from_invocation': {},
            'from_default': {
                'default': 'from_default_default'
            },
            'invocation_overrides_default': {
                'default': 'invocation_overrides_default_default'
            }
        }

        normal_execute_kwargs = {
            'parameters': {
                'from_invocation': 'from_invocation',
                'invocation_overrides_default':
                'invocation_overrides_default_override'
            }
        }

        def normal_flow(ctx,
                        from_invocation,
                        from_default,
                        invocation_overrides_default,
                        **_):
            self.assertEqual(from_invocation, 'from_invocation')
            self.assertEqual(from_default, 'from_default_default')
            self.assertEqual(invocation_overrides_default,
                             'invocation_overrides_default_override')

        self._execute_workflow(normal_flow,
                               execute_kwargs=normal_execute_kwargs,
                               workflow_parameters_schema=normal_schema,
                               use_existing_env=False)

        # now test missing
        missing_schema = normal_schema.copy()
        missing_schema['missing_parameter'] = {}
        missing_flow = normal_flow
        missing_execute_kwargs = normal_execute_kwargs
        self.assertRaises(ValueError,
                          self._execute_workflow,
                          missing_flow,
                          execute_kwargs=missing_execute_kwargs,
                          workflow_parameters_schema=missing_schema,
                          use_existing_env=False)

        # now test invalid custom parameters
        invalid_custom_schema = normal_schema
        invalid_custom_flow = normal_flow
        invalid_custom_kwargs = normal_execute_kwargs.copy()
        invalid_custom_kwargs['parameters']['custom_parameter'] = 'custom'
        self.assertRaises(ValueError,
                          self._execute_workflow,
                          invalid_custom_flow,
                          execute_kwargs=invalid_custom_kwargs,
                          workflow_parameters_schema=invalid_custom_schema,
                          use_existing_env=False)

        # now test valid custom parameters
        def valid_custom_flow(ctx,
                              from_invocation,
                              from_default,
                              invocation_overrides_default,
                              custom_parameter,
                              **_):
            self.assertEqual(from_invocation, 'from_invocation')
            self.assertEqual(from_default, 'from_default_default')
            self.assertEqual(invocation_overrides_default,
                             'invocation_overrides_default_override')
            self.assertEqual(custom_parameter, 'custom')

        valid_custom_schema = normal_schema
        valid_custom_kwargs = normal_execute_kwargs.copy()
        valid_custom_kwargs['parameters']['custom_parameter'] = 'custom'
        valid_custom_kwargs['allow_custom_parameters'] = True
        self._execute_workflow(
            valid_custom_flow,
            execute_kwargs=valid_custom_kwargs,
            workflow_parameters_schema=valid_custom_schema,
            use_existing_env=False)

    def test_retry_configuration(self):
        retry_interval = 0.1
        task_retries = 1

        def flow(ctx, **_):
            instance = _instance(ctx, 'node')
            instance.execute_operation('test.op0').get()
            instance.execute_operation('test.op1').get()

        def op0(ctx, **_):
            self.assertIsNotNone(ctx.node_id)
            current_retry = ctx.runtime_properties.get('retry', 0)
            last_timestamp = ctx.runtime_properties.get('timestamp')
            current_timestamp = time.time()

            ctx.runtime_properties['retry'] = current_retry + 1
            ctx.runtime_properties['timestamp'] = current_timestamp

            if current_retry > 0:
                self.assertLess(current_timestamp - last_timestamp, 0.5)
            if current_retry < task_retries:
                self.fail()

        def op1(ctx, **_):
            self.assertEqual(task_retries + 1, ctx.runtime_properties['retry'])

        self._execute_workflow(
            flow,
            operation_methods=[op0, op1],
            execute_kwargs={
                'task_retry_interval': retry_interval,
                'task_retries': task_retries})

    def test_local_task_thread_pool_size(self):
        default_size = workflow_context.DEFAULT_LOCAL_TASK_THREAD_POOL_SIZE

        def flow(ctx, **_):
            task_processor = ctx.internal.local_tasks_processor
            self.assertEqual(len(task_processor._local_task_processing_pool),
                             default_size)
        self._execute_workflow(
            flow,
            use_existing_env=False)

        def flow(ctx, **_):
            task_processor = ctx.internal.local_tasks_processor
            self.assertEqual(len(task_processor._local_task_processing_pool),
                             default_size + 1)
        self._execute_workflow(
            flow,
            execute_kwargs={'task_thread_pool_size': default_size + 1},
            use_existing_env=False)

    def test_invalid_storage_class(self):
        def flow(ctx, **_):
            pass
        self.storage_cls = local.Storage
        self.assertRaises(ValueError,
                          self._execute_workflow, flow)
        self.storage_cls = self.__class__
        self.assertRaises(ValueError,
                          self._execute_workflow, flow)

    def test_no_operation_module(self):
        self._no_module_or_attribute_test(
            is_missing_module=True,
            test_type='operation')

    def test_no_operation_attribute(self):
        self._no_module_or_attribute_test(
            is_missing_module=False,
            test_type='operation')

    def test_no_source_operation_module(self):
        self._no_module_or_attribute_test(
            is_missing_module=True,
            test_type='source')

    def test_no_source_operation_attribute(self):
        self._no_module_or_attribute_test(
            is_missing_module=False,
            test_type='source')

    def test_no_target_operation_module(self):
        self._no_module_or_attribute_test(
            is_missing_module=True,
            test_type='target')

    def test_no_target_operation_attribute(self):
        self._no_module_or_attribute_test(
            is_missing_module=False,
            test_type='target')

    def test_no_workflow_module(self):
        self._no_module_or_attribute_test(
            is_missing_module=True,
            test_type='workflow')

    def test_no_workflow_attribute(self):
        self._no_module_or_attribute_test(
            is_missing_module=False,
            test_type='workflow')

    def test_no_workflow(self):
        try:
            self._execute_workflow(workflow_name='does_not_exist')
            self.fail()
        except ValueError, e:
            self.assertIn("['workflow']", e.message)

    def _no_module_or_attribute_test(self, is_missing_module, test_type):
        try:
            self._execute_workflow(
                create_blueprint_func=self._blueprint_2(is_missing_module,
                                                        test_type))
            self.fail()
        except ImportError, e:
            if is_missing_module:
                self.assertIn('No module named zzz', e.message)
                self.assertIn(test_type, e.message)
            else:
                raise
        except AttributeError, e:
            if not is_missing_module:
                self.assertIn("has no attribute 'does_not_exist'", e.message)
                self.assertIn(test_type, e.message)
            else:
                raise

    def _blueprint_2(self,
                     is_missing_module,
                     test_type):
        def func(*_):
            module_name = 'zzz' if is_missing_module else self._testMethodName
            interfaces = {
                'test': [
                    {'op': 'p.{}.{}'.format(module_name, 'does_not_exist')}
                ]
            }
            blueprint = {
                'plugins': {
                    'p': {
                        'derived_from': 'cloudify.plugins.manager_plugin'
                    }
                },
                'node_types': {
                    'type': {}
                },
                'relationships': {
                    'cloudify.relationships.contained_in': {}
                },
                'node_templates': {
                    'node2': {
                        'type': 'type',
                    },
                    'node': {
                        'type': 'type',
                        'relationships': [{
                            'target': 'node2',
                            'type': 'cloudify.relationships.contained_in',
                        }]
                    },
                },
                'workflows': {
                    'workflow': 'p.{}.{}'.format(module_name,
                                                 'does_not_exist')
                }
            }

            node = blueprint['node_templates']['node']
            relationship = node['relationships'][0]
            if test_type == 'operation':
                node['interfaces'] = interfaces
            elif test_type == 'source':
                relationship['source_interfaces'] = interfaces
            elif test_type == 'target':
                relationship['target_interfaces'] = interfaces
            elif test_type == 'workflow':
                pass
            else:
                self.fail('unsupported: {}'.format(test_type))

            return blueprint
        return func


def _instance(ctx, node_name):
    return next(ctx.get_node(node_name).instances)