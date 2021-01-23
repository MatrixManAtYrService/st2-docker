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
# This pipeline does something altogether stranger. It injects local code into
# the containerized services at runtime by mounting parts of the dev's env at
# /opt/stackstorm/st2/lib/python3.6/site-packages/*
#
# Between "up" and "down" it runs a very limited sanity check just to ensure
# that the services did indeed start.
#
# Pipeline nodes are just python objects, so if you're working in some other
# repo and want a pipeline that does:
# -"up"
# -"run tests specific to this repo's contribution"
# -"down"
# you can import this file, take the up and down nodes from the object returned
# by updown(), and construct your own pipeline with juicier bits in the middle.

# #### Usage ####
#
# first:
#     - create a conducto account (they're free)
#     - from the conducto web UI, start a local agent
#     $ pip install conducto # unless you already have
#
# stage released-changes only:
#     $ python pipeline.py updown --local
#
# stage with dev-changes:
#     $ python pipeline.py updown --st2path /path/to/dev/st2 --local


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
    image_parent = "/opt/stackstorm/st2/lib/python3.6/site-packages/"
    return {
        "st2api": f"{image_parent}/st2api:{dev_path}/st2api/st2api",
        "st2reactor": f"{image_parent}/st2reactor:{dev_path}/st2reactor/st2reactor",
        "st2rulesengine": f"{image_parent}/st2rulesengine:{dev_path}/st2rulesengine/st2rulesengine",
        "st2common": f"{image_parent}/st2common:{dev_path}/st2common/st2common",
        "st2client": f"{image_parent}/st2client:{dev_path}/st2client/st2client",
    }

def lazy_tests() -> co.Serial:
    """
    These tests are created by a Lazy node (i.e. after the initial pipeline 
    kicks off) this lets compose.ip() reference $CONDUCTO_PIPELINE_ID, which
    isn't available until after the pipeline has launched.
    There's probably a better way.
    """

    # use some other image for these, just for fun
    img = co.Image(image="alpine:latest", reqs_py=["conducto"])

    node = co.Parallel(image=img)
    for service in ["st2api", "st2auth"]:
        ip = compose.ip("st2api").conducto
        node[f"ping {service}"] = co.Exec(f"ping -c 2 {ip}")
    return node

def all_tests() -> co.Parallel:

    # do these in parallel
    node = co.Parallel()

    # an st2 call via docker-compose
    node["call st2client via compose"] = \
        compose.exec("st2client", "st2 action list --pack=core")

    # like above, but without syntactic sugar and with an assert
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

def updown(st2path=None) -> co.Serial:
    """
    Returns the root of a pipeline tree which sets up, performs some tests, and tears down
    """

    # are we staging dev artifacts?
    source = Path("../docker-compose.yml").resolve()
    if st2path:
        compose.stage(source, volumes=get_dev_mounts(st2path))
    else:
        compose.stage(source)

    # generate docker-compose.yml.g

    # use an image with docker-compose in it
    img = co.Image(
        image=compose.image, copy_dir="..", reqs_py=compose.reqs_py
    )
    root = co.Serial(image=img, requires_docker=True)

    # set up environment
    root["up"] = compose.up()
    root["up/wait for actually up"] = co.Exec(
        """
        echo TODO: instead of sleeping, watch logs and detect the "all-the-way-up" state
        sleep 20
        """)

    # clean up, even on failures
    root["go"] = co.Serial(stop_on_error=False)
    root["go/all tests"] = all_tests()
    root["go/down"] = compose.down()

    # unless we injected local code
    if st2path:
        root["go/down"].skip = True
        # unpause this node to clean up
        # or run:
        #   $ docker-compose -f docker-compose.g.yml down

    return root


if __name__ == "__main__":
    co.main()
