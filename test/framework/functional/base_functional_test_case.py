from contextlib import suppress
import os
from os import path
import shutil
import tempfile
from unittest import TestCase

from clusterrunner.app.util import log
from clusterrunner.app.util.process_utils import is_windows
from clusterrunner.app.util.secret import Secret
from test.framework.functional.fs_item import Directory
from test.framework.functional.functional_test_cluster import FunctionalTestCluster, TestClusterTimeoutError


class BaseFunctionalTestCase(TestCase):
    """
    This is the base class for all functional tests. This class has two main purposes:
        - Make available a `FunctionalTestCluster` object for use in functional tests (self.cluster)
        - Implement any helper assertion methods that might be useful for making our tests easier to read and write
    """
    def setUp(self):
        # Configure logging to go to stdout. This makes debugging easier by allowing us to see logs for failed tests.
        log.configure_logging('DEBUG')

        Secret.set('testsecret')

        self.cluster = FunctionalTestCluster(verbose=self._get_test_verbosity())

    def _create_test_config_file(self, conf_values_to_set=None):
        """
        Create a temporary conf file just for this test.

        :return: The path to the conf file
        :rtype: str
        """
        # Copy default conf file to tmp location
        repo_dir = path.dirname(path.dirname(path.dirname(path.dirname(path.realpath(__file__)))))
        self._conf_template_path = path.join(repo_dir, 'conf', 'default_clusterrunner.conf')
        test_conf_file_path = tempfile.NamedTemporaryFile().name
        shutil.copy(self._conf_template_path, test_conf_file_path)
        os.chmod(test_conf_file_path, ConfigFile.CONFIG_FILE_MODE)
        conf_file = ConfigFile(test_conf_file_path)

        # Set custom conf file values for this test
        conf_values_to_set = conf_values_to_set or {}
        for conf_key, conf_value in conf_values_to_set.items():
            conf_file.write_value(conf_key, conf_value, BASE_CONFIG_FILE_SECTION)

        return test_conf_file_path

    def tearDown(self):
        # Give the cluster a bit of extra time to finish working (before forcefully killing it and failing the test)
        with suppress(TestClusterTimeoutError):
            self.cluster.block_until_build_queue_empty(timeout=5)

        # Kill processes and make sure all processes exited with 0 exit code
        services = self.cluster.kill()

        # only check the exit code if not on Windows as Popen.terminate kills the process on Windows and the exit
        # code is not zero.
        # TODO: remove the is_windows() check after we can handle exit on Windows gracefully.
        if not is_windows():
            for service in services:
                self.assertEqual(
                    service.return_code,
                    0,
                    'Service running on url: {} should exit with code 0, but exited with code {}.'.format(
                        service.url,
                        service.return_code,
                    ),
                )
        # Remove the temp dir. This will delete the log files, so should be run after cluster shuts down.
        self.cluster.master_app_base_dir.cleanup()
        [slave_app_base_dir.cleanup() for slave_app_base_dir in self.cluster.slaves_app_base_dirs]

    def _get_test_verbosity(self):
        """
        Get test verbosity from an env variable. We need to use an env var since Nose does not support specifying
        command-line test configuration natively. (But if we need more of these configuration paramaters, we should
        instead look at the 'nose-testconfig' plugin instead of adding tons of environment variables.)

        :return: Whether or not tests should be run verbosely
        :rtype: bool
        """
        is_verbose = os.getenv('CR_VERBOSE') not in ('0', '', None)  # default value of is_verbose is False
        return is_verbose

    def assert_build_status_contains_expected_data(self, build_id, expected_data):
        """
        Assert that the build status endpoint contains the expected fields and values. This assertion does an API
        request to the master service of self.cluster.

        :param build_id: The id of the build whose status to check
        :type build_id: int
        :param expected_data: A dict of expected keys and values in the build status response
        :type expected_data: dict
        """
        build_status = self.cluster.master_api_client.get_build_status(build_id).get('build')
        self.assertIsInstance(build_status, dict, 'Build status API request should return a dict.')
        self.assertDictContainsSubset(expected_data, build_status,
                                      'Build status API response should contain the expected status data.')

    def assert_build_has_successful_status(self, build_id):
        """
        Assert that the build status endpoint contains fields signifying the build was successful (had no failures).
        This assertion does an API request to the master service of self.cluster.

        :param build_id: The id of the build whose status to check
        :type build_id: int
        """
        expected_successful_build_params = {
            'result': 'NO_FAILURES',
            'status': 'FINISHED',
        }
        self.assert_build_status_contains_expected_data(build_id, expected_successful_build_params)

    def assert_build_has_failure_status(self, build_id):
        """
        Assert that the build status endpoint contains fields signifying the build was failed. This assertion does an
        API request to the master service of self.cluster.

        :param build_id: The id of the build whose status to check
        :type build_id: int
        """
        expected_failure_build_params = {
            'result': 'FAILURE',
            'status': 'FINISHED',
        }
        self.assert_build_status_contains_expected_data(build_id, expected_failure_build_params)

    def assert_build_has_canceled_status(self, build_id):
        """
        Assert that the build status endpoint contains fields signifying the build was failed. This assertion does an
        API request to the master service of self.cluster.

        :param build_id: The id of the build whose status to check
        :type build_id: int
        """
        expected_failure_build_params = {
            'result': 'FAILURE',
            'status': 'CANCELED',
            }
        self.assert_build_status_contains_expected_data(build_id, expected_failure_build_params)

    def assert_build_artifact_contents_match_expected(self, build_id, expected_build_artifact_contents):
        """
        Assert that artifact files for this build have the expected contents.

        :param build_id: The id of the build whose artifacts to check
        :type build_id: int
        :param expected_build_artifact_contents: A list of FSItems corresponding to the expected artifact dir contents
        :type expected_build_artifact_contents: list[FSItem]
        """
        build_artifacts_dir_path = os.path.join(self.cluster.master_app_base_dir.name, 'results', 'master', str(build_id))
        self.assert_directory_contents_match_expected(build_artifacts_dir_path, expected_build_artifact_contents)

    def assert_directory_contents_match_expected(self, dir_path, expected_dir_contents):
        """
        Assert that the specified directory has the expected contents.

        :param dir_path: The path of the directory whose artifacts to check
        :type dir_path: string
        :param expected_dir_contents: A list of FSItems corresponding to the expected directory contents
        :type expected_dir_contents: list[FSItem]
        """
        if expected_dir_contents is not None:
            dir_path = os.path.abspath(dir_path)  # converts path to absolute, removes trailing slash if present
            expected_dir_name = os.path.basename(dir_path)
            expected_build_artifacts = Directory(expected_dir_name, expected_dir_contents)
            expected_build_artifacts.assert_matches_path(dir_path, allow_extra_items=False)
