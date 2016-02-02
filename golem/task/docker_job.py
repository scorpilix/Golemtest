from docker import Client
from docker import errors

from os import path


class DockerImage(object):

    def __init__(self, repository, id=None, tag=None):
        self.repository = repository
        self.id = id
        self.tag = tag if tag else "latest"
        self.name = "{}:{}".format(self.repository, self.tag)
        if not self._check():
            raise ValueError("Image name does not match image ID")

    def _check(self):
        client = Client()
        if self.id:
            info = client.inspect_image(self.id)
        else:
            info = client.inspect_image(self.name)
        # Check that name and ID agree
        assert info
        return self.name in info["RepoTags"] and (
            self.id is None or info["Id"] == self.id)

    @staticmethod
    def is_available(repository, id=None, tag=None):
        try:
            image = DockerImage(repository, id=id, tag=tag)
            return image._check()
        except errors.NotFound:
            return False
        except errors.APIError as e:
            if tag is not None:
                return False
            raise e
        except ValueError:
            return False


class DockerJob(object):

    STATE_NEW = "new"
    STATE_CREATED = "created"  # container created by docker
    STATE_RUNNING = "running"  # docker container running
    STATE_EXITED = "exited"    # docker container finished running
    STATE_STOPPED = "stopped"
    STATE_KILLED = "killed"
    STATE_REMOVED = "removed"

    # name of the script file, relative to the task dir
    TASK_SCRIPT = "job.py"
    # name of the parameters file, relative to the task dir
    PARAMS_FILE = "params.py"

    RESOURCES_DIR = "/golem/resources/"
    OUTPUT_DIR = "/golem/output/"

    def __init__(self, image, script_src, parameters,
                 work_dir, resource_dir, output_dir):
        """
        :param DockerImage image: Docker image to use
        :param str script_src: source of the script file
        :param str output_dir:
        :param str resource_dir:
        """
        self.image = image
        self.script_src = script_src
        self.parameters = parameters if parameters else {}
        self.work_dir = work_dir
        self.resource_dir = resource_dir
        self.output_dir = output_dir

        self.task_dir = self.resource_dir + "/" + self.work_dir
        self.container = None
        self.container_id = None
        self.container_log = None
        self.state = self.STATE_NEW

    def _prepare(self):
        # Save parameters in task_dir/PARAMS_FILE
        if self.parameters:
            params_file_path = self._get_params_path()
            with open(params_file_path, "w") as params_file:
                for key, value in self.parameters.iteritems():
                    params_file.write("{} = {}\n".format(key, repr(value)))
            self.script_src = "from params import *\n\n" + self.script_src

        # Save the script in task_dir/TASK_SCRIPT
        task_script_path = self._get_script_path()
        with open(task_script_path, "w") as script_file:
            script_file.write(self.script_src)

        # Setup volumes for the container
        client = Client()
        host_cfg = client.create_host_config(
            binds={
                self.resource_dir: {
                    "bind": self.RESOURCES_DIR,
                    "mode": "ro"
                },
                self.output_dir: {
                    "bind": self.OUTPUT_DIR,
                    "mode": "rw"
                }
            }
        )

        self.container = client.create_container(
            image=self.image.name,
            volumes=[self.RESOURCES_DIR, self.OUTPUT_DIR],
            host_config=host_cfg,
            network_disabled=True,
            entrypoint=["/usr/bin/python", "job.py"],
            working_dir=self.RESOURCES_DIR + "/" + self.work_dir)

        self.container_id = self.container["Id"]
        assert self.container_id

    def _cleanup(self):
        """Removes the temporary directory task_dir"""
        if self.container:
            client = Client()
            if self.get_status() == self.STATE_RUNNING:
                client.kill(self.container_id)
                self.state = self.STATE_KILLED
            client.remove_container(self.container_id, force=True)
            self.container = None
            self.container_id = None
            self.state = self.STATE_REMOVED

    def __enter__(self):
        self._prepare()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._cleanup()

    def _get_script_path(self):
        return path.join(self.task_dir, self.TASK_SCRIPT)

    def _get_params_path(self):
        return path.join(self.task_dir, self.PARAMS_FILE)

    def start(self):
        if self.get_status() == self.STATE_CREATED:
            client = Client()
            client.start(self.container_id)
            result = client.inspect_container(self.container_id)
            self.state = result["State"]["Status"]
            return result
        return None

    def wait(self, timeout=None):
        """Block until the job completes, or timeout elapses.
        :param timeout: time to block
        :returns container exit code
        """
        if self.get_status() in [self.STATE_RUNNING, self.STATE_EXITED]:
            client = Client()
            return client.wait(self.container_id, timeout)
        return -1

    def get_logs(self):
        if self.container:
            client = Client()
            return client.logs(self.container_id, stdout=True, stderr=True)

    def get_status(self):
        if self.container:
            client = Client()
            inspect = client.inspect_container(self.container_id)
            return inspect["State"]["Status"]
        return self.state
