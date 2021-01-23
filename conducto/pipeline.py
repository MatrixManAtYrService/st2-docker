import conducto as co
import compose
from pathlib import Path
import yaml

# #### What is this? ####
#
# The docker-compose.yml at the root of this repo is set up to run
# released bits only.  As is, it's not helpful for trying out uncommitted
# changes.  Maybe an enirely separate docker-compose.yml for
# in-development bits is the right way to go. So instead of doing this...
# https://github.com/StackStorm/st2-dockerfiles/blob/d6ee9ce0735e6079e732aa762d02d459e9863851/base/Dockerfile#L42
# ...such a file would live in the st2 repo and use COPY dockerfile
# directives to build the local code into the services that it ran.
#
# That would be the way to go if somebody was willing to maintain parity
# between the two.
#
# This pipeline does something altogether stranger. It uses the structure
# defined by the release-only docker-compose.yml, and injects local code into
# the containerized services at runtime by mounting parts of the dev's env at
# /opt/stackstorm/st2/lib/python3.6/site-packages/*
#
# Between "up" and "down" it runs a very limited sanity check just to ensure
# that the services did indeed start and to give you a gist about how you would
# add more tests.


# #### Usage ####
#
# first:
#     - create a conducto account (they're free)
#     - start docker
#     - from the conducto web UI, copy the local agent start command
#     - paste it into a shell
#     $ pip install conducto pyyaml
#
# dry-run for released changes
#     $ python conducto/pipeline.py updown
#
# dry-run for dev changes
#     $ python conducto/pipeline.py updown /path/to/dev/st2
#
# generate and run with released-changes only:
#     $ python conducto/pipeline.py updown --local --run
#
# generate and run with dev changes only:
#     $ python conducto/pipeline.py updown /path/to/dev/st2 --local --run
#
# The commands above create ./docker-compose.yml.g which is ued by the pipeline
# below, but you can also use it outside of conducto to control service state:
#
#     docker-compose -f docker-compose.g.yml -p foo-bar up
#     docker-compose -f ... ps
#     docker-compose -f ... down
#     docker-compose -f ... exec st2auth /bin/bash -c 'cat /etc/st2/htpasswd'
#     docker-compose -f ... exec st2client /bin/bash -i

def get_dev_mounts(st2_path):
    """
    Returns a dictionary that maps local dev files to their locations the
    services that docker-compose manages.
    """

    # sanity check the path
    dev_path = Path(st2_path).resolve()
    if not (dev_path / "st2api").is_dir():
        raise FileNotFoundError(f"{dev_path} should be the root of the st2 repo")

    # docker image source parent
    image_parent = "/opt/stackstorm/st2/lib/python3.6/site-packages"
    return {
        "st2api": f"{dev_path}/st2api/st2api:{image_parent}/st2api",
        "st2reactor": f"{dev_path}/st2reactor/st2reactor:{image_parent}/st2reactor",
        "st2rulesengine": f"{dev_path}/st2rulesengine/st2rulesengine:{image_parent}/st2rulesengine",
        "st2common": f"{dev_path}/st2common/st2common:{image_parent}/st2common",
        "st2client": f"{dev_path}/st2client/st2client:{image_parent}/st2client",
    }


def updown(st2path=None) -> co.Serial:
    """
    Defines the pipeline
    """

    # generate docker-compose.yml.g
    source = (Path(__file__).parent / ".." / "docker-compose.yml").resolve()
    if st2path:
        # stage dev artifacts?
        compose.stage(source, volumes=get_dev_mounts(st2path))
    else:
        # use released images
        compose.stage(source)

    img = co.Image(image=compose.image, copy_dir="..", reqs_py=compose.reqs_py)

    root = co.Serial(image=img, requires_docker=True)

    # set up environment
    root["up"] = compose.up()
    root["up/wait for actually up"] = co.Exec(
        """
        echo TODO: instead of sleeping, watch logs and detect the "all-the-way-up" state
        sleep 20
        """
    )

    # clean up, even on failures
    root["go"] = co.Serial(stop_on_error=False)
    root["go/all tests"] = all_tests()
    root["go/down"] = compose.down()

    # unless we injected local code
    if st2path:
        root["go/down"].skip = True
        # unpause this node to clean up
        # or run:
        #   $ docker-compose -f docker-compose.g.yml -p conducto down

    return root


# just to demonstrate that we don't need to do everything though docker-compose
other_img = co.Image(
    image="alpine:latest", reqs_py=["conducto"], reqs_packages=["curl"]
)


def all_tests() -> co.Parallel:

    node = co.Parallel()
    node["call st2client via compose"] = compose.exec(
        "st2client", "st2 action list --pack=core"
    )

    # like above, miunus the syntactic sugar, plus an assert
    node["assert st2client response"] = co.Exec(
        f"""
        set -e
        {compose.cmd} exec -T st2client \\
            st2 action list --pack=core \\
            | grep 'local_sudo'
        """
    )

    node["lazy tests"] = co.Lazy(lazy_tests)

    return node


def lazy_tests() -> co.Serial:
    """
    These tests are created by a Lazy node (i.e. after the initial
    pipeline kicks off).  This lets compose.ip() reference
    $CONDUCTO_PIPELINE_ID and determine the IP addresses of the
    compose-managed services--both of which aren't known at
    pipeline-lauch time.
    """

    # use some other image for these, just for fun

    node = co.Parallel(image=other_img)
    for service in ["st2api", "st2auth"]:
        ip = compose.ip("st2api").conducto
        node[f"ping {service}"] = co.Exec(f"ping -c 2 {ip}")
    return node


if __name__ == "__main__":
    co.main()


# #### Gotchas ####
#
# This pipeline won't run with --cloud because the docker daemon from /up gets
# cleaned up before /down runs.  Also, docker-compose networking doesn't work
# between separate docker daemons.  Conducto is considering solutions for this,
# In the meantime you can just set up a box in aws and run a conducto agent
# there.  conducto.com will relay events from github to local agents just the
# same as it would for the --cloud case.
#
# It's kind of weird to have this in st2-docker when the code being
# tested is in st2, but the only actual interaction with st2 is the volume
# mount.  If it were me, I'd make st2-docker a git submodule
# of st2.  That way this repo remains unchanged, that repo contains the
# pipeline, and it can reference the env-setup stuff here by just
# pulling submodules before launchig the pipeline.  Many people don't
# like git submodules as much as I do though, so I put it here since it
# let me get away with touching only one repo.

# There are lots of options here, but the only hard constraint is that the
# volume mounts need to be absolute paths on the host--not in a container.  So
# we can't, for instance, clone st2-docker in a pipeline node and then mount
# its contents via docker-compose.  But as long as we can reference both repo's at
# pipeline launch time, any mix/match should be possible.
