import json
from textwrap import indent
from collections import namedtuple
from pathlib import Path
import conducto as co
import os

image = "docker/compose"
reqs_py = ["conducto", "docker-py", "sh", "pyyaml"]

# generate this file (stash it next to docker-compose.yml)
staged = ".docker-compose.yml.g"

# prefix commands with this to use the staged dockerfile
cmd = "docker-compose -f " + staged


def inspect(service_name, print_data=False):
    """
    If 'up' was called in this pipeline inspect details will be stashed
    Get the stashed details
    """

    key = f"/services/{service_name}/inspect"
    data_str = co.data.pipeline.gets(key).decode()
    data = json.loads(data_str)
    if print_data:
        print(json.dumps(data, indent=2))
    return data


IPs = namedtuple("IPs", "conducto others")


def ip(service_name, print_data=False):
    """
    If 'up' was called in this pipeline inspect details will be stashed
    Get the IP address for this service from the stashed inspect
    """
    pipeline_id = os.environ["CONDUCTO_PIPELINE_ID"]

    data = inspect(service_name)
    conducto = None
    others = []
    network_names = []
    for name, network in data["NetworkSettings"]["Networks"].items():
        network_names.append(name)
        if pipeline_id in name:
            conducto = network["IPAddress"]
        else:
            others.append(network["IPAddress"])

    if not conducto:
        raise Exception(
            f"CONDUCTO_PIPELINE_ID = {pipeline_id}, but no matching network was found among {json.dumps(network_names)}"
        )

    val = IPs(conducto, others)
    if print_data:
        print(val)
    return val


def down(path_to_yml=staged) -> co.Exec:
    return co.Exec(f"docker-compose -f {path_to_yml} down")

def exec(service, command, path_to_yml=staged) -> co.Exec:
    return co.Exec(f"docker-compose -f {path_to_yml} exec -T {service} {command}")

def _print_val(key, getfunc, json=False):
    """
    Describe what we stored
    """
    if not json:
        print(
            key,
            "=",
            indent(getfunc(key).decode(), "    "),
        )
    else:
        print(
            key,
            "=",
            indent(json.dumps(json.loads(getfunc(key).decode()), indent=2), "    "),
        )


def _store_container_info(key, datum):
    if type(datum) == str:
        co.data.pipeline.puts(key, datum.encode())
        _print_val(key, co.data.pipeline.gets, json=False)
    else:
        co.data.pipeline.puts(key, json.dumps(datum).encode())
        _print_val(key, co.data.pipeline.gets)

def up(path_to_yml=staged) -> co.Serial:
    node = co.Serial()
    node["start services"] = co.Exec(f"docker-compose -f {path_to_yml} up -d")
    node["collect metadata"] = co.Exec(examine, path_to_yml)
    return node

def examine(path_to_yml):
    """
    Set up the services defined in the indicated docker-compose.yml
    """
    import docker
    from sh import docker_compose

    # reference this docker-compose file
    def args(*compose_args):
        return ["-f", path_to_yml] + list(compose_args)

    # start services if not started
    service_names = (
        docker_compose(args("ps", "--services"), _tee=True).strip().split("\n")
    )
    container_ids = docker_compose(args("ps", "-q"), _tee=True).strip().split("\n")

    # associate the service names with their runtime details
    inspections_by_service_name = {}
    client = docker.Client()
    for container_id in container_ids:

        inspection = client.inspect_container(container_id)
        container_name = inspection["Name"]

        for service_name in service_names:
            if service_name in container_name:
                inspections_by_service_name[service_name] = inspection
                container_status = inspection["State"]["Status"]

                # store full inspection
                _store_container_info(f"/services/{service_name}/inspect", inspection)
                _store_container_info(
                    f"/services/{service_name}/status", container_status
                )

                # pull out ip's if container remains up
                if container_status == "running":

                    # store ip's separately for easy retrieval
                    service_ips = ip(service_name)
                    _store_container_info(
                        f"/services/{service_name}/ip/others", service_ips.others
                    )
                    _store_container_info(
                        f"/services/{service_name}/ip/conducto", service_ips.conducto
                    )
                break


def anchored(content, path):
    """
    docker-in-docker quirk: the left side of a volume mount string is
    interpreted to refer to files on the outermost host, not in the
    container where the command runs. This causes problems when
    docker-compose.yml files with relative paths in volume mounts are
    copied into images and called from containers based on those images.

    hack 1: replace relative paths in the docker-compose file with
    absolute paths on the outermost host so that if this file ends up
    being referenced from a container, it still points to the same
    files.

    conducto-quirk: pipeline nodes aren't networked such that they can
    talk to just any old container (like the ones that docker-compose
    creates).

    hack 2: ensure that docker-compose adds the conducto-pipeline-node
    network to each service so that the nodes can talk directly to each
    docker-compose managed service.

    parameter 'content': a dictionary containing the guts of docker-compose.yml
    parameter 'path': an absolute path pointing to docker-compose.yml on the host
    return: altered guts for a conducto-friendly docker-compose.yml
    """

    # don't alter the original
    content = content.copy()

    path = Path(path).parent

    # apply hack 1
    for service_name, service_def in content["services"].items():
        try:
            for i, volume in enumerate(service_def["volumes"]):
                items = volume.split(":")
                daemonside = items[0]
                containerside = ":".join(items[1:])
                if daemonside[0:2] == "./":
                    daemonside = f"{path}/{daemonside[2:]}"
                    mountstr = ":".join([daemonside, containerside])
                    service_def["volumes"][i] = mountstr
        except KeyError:
            pass

    # TODO: other relative paths that we might want to anchor:
    # services.foo.build
    # services.foo.build.context
    # Or maybe it's better to just create a path in the image that's identical
    # to the host and put it there?

    # apply hack 2
    conducto_network = content.setdefault("networks", {}).setdefault("conducto", {})
    conducto_network["external"] = {"name": "conducto_network_$CONDUCTO_PIPELINE_ID"}
    for service_name, service_def in content["services"].items():
        service_networks = content["services"][service_name].setdefault("networks", [])
        if "conducto" not in service_networks:
            service_networks.append("conducto")

    return content


def stage(host_compose, image_compose=None, volumes={}):
    """
    Find docker-compose.yml and generate docker-compose.yml.g with
    necessary changes
    """

    import yaml
    from pathlib import Path

    # situate files
    if image_compose:
        # if we're in a container, use the path in the image
        source = Path(image_compose).resolve()
    else:
        # if we're not in a container, use the path on the host
        source = Path(host_compose).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Can't find {source}")
    parent = source.parent
    target = parent / staged

    # read
    with open(source, "r") as f:
        content = yaml.load(f, Loader=yaml.FullLoader)

    # mutate
    altered = anchored(content, host_compose)

    # write
    with open(target, "w") as f:
        yaml.dump(altered, f)
        print(f"wrote {target}")
