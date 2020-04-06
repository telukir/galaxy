"""This module describes the :class:`MulledContainerResolver` ContainerResolver plugin."""

import collections
import logging
import os
import subprocess

import six

from galaxy.util import (
    string_as_bool,
    unicodify,
)
from ..commands import shell
from ..container_classes import CONTAINER_CLASSES
from ..container_resolvers import (
    ContainerResolver,
)
from ..docker_util import build_docker_images_command
from ..mulled.mulled_build import (
    DEFAULT_CHANNELS,
    ensure_installed,
    InvolucroContext,
    mull_targets,
)
from ..mulled.mulled_build_tool import requirements_to_mulled_targets
from ..mulled.util import (
    mulled_tags_for,
    split_tag,
    v1_image_name,
    v2_image_name,
)
from ..requirements import (
    ContainerDescription,
    DEFAULT_CONTAINER_SHELL,
)

log = logging.getLogger(__name__)


CachedMulledImageSingleTarget = collections.namedtuple("CachedMulledImageSingleTarget", ["package_name", "version", "build", "image_identifier"])
CachedV1MulledImageMultiTarget = collections.namedtuple("CachedV1MulledImageMultiTarget", ["hash", "build", "image_identifier"])
CachedV2MulledImageMultiTarget = collections.namedtuple("CachedV2MulledImageMultiTarget", ["image_name", "version_hash", "build", "image_identifier"])

CachedMulledImageSingleTarget.multi_target = False
CachedV1MulledImageMultiTarget.multi_target = "v1"
CachedV2MulledImageMultiTarget.multi_target = "v2"


@property
def _package_hash(target):
    # Make this work for Singularity file name or fully qualified Docker repository
    # image names.
    image_name = target.image_name
    if "/" not in image_name:
        return image_name
    else:
        return image_name.rsplit("/")[-1]


CachedV2MulledImageMultiTarget.package_hash = _package_hash


def list_docker_cached_mulled_images(namespace=None, hash_func="v2", resolution_cache=None):
    cache_key = "galaxy.tool_util.deps.container_resolvers.mulled:cached_images"
    if resolution_cache is not None and cache_key in resolution_cache:
        images_and_versions = resolution_cache.get(cache_key)
    else:
        command = build_docker_images_command(truncate=True, sudo=False, to_str=False)
        try:
            images_and_versions = unicodify(subprocess.check_output(command)).strip().splitlines()
        except subprocess.CalledProcessError:
            log.info("Call to `docker images` failed, configured container resolution may be broken")
            return []
        images_and_versions = [l.split()[0:2] for l in images_and_versions[1:]]
        if resolution_cache is not None:
            resolution_cache[cache_key] = images_and_versions

    name_filter = get_filter(namespace)

    def output_line_to_image(line):
        image_name, version = line[0], line[1]
        identifier = "%s:%s" % (image_name, version)
        image = identifier_to_cached_target(identifier, hash_func, namespace=namespace)
        return image

    raw_images = [output_line_to_image(_) for _ in filter(name_filter, images_and_versions)]
    return [i for i in raw_images if i is not None]


def identifier_to_cached_target(identifier, hash_func, namespace=None):
    if ":" in identifier:
        image_name, version = identifier.rsplit(":", 1)
    else:
        image_name = identifier
        version = None

    if not version or version == "latest":
        version = None

    image = None
    prefix = ""
    if namespace is not None:
        prefix = "quay.io/%s/" % namespace
    if image_name.startswith(prefix + "mulled-v1-"):
        if hash_func == "v2":
            return None

        hash = image_name
        build = None
        if version and version.isdigit():
            build = version
        image = CachedV1MulledImageMultiTarget(hash, build, identifier)
    elif image_name.startswith(prefix + "mulled-v2-"):
        if hash_func == "v1":
            return None

        version_hash = None
        build = None

        if version and "-" in version:
            version_hash, build = version.rsplit("-", 1)
        elif version.isdigit():
            version_hash, build = None, version
        elif version:
            log.debug("Unparsable mulled image tag encountered [%s]" % version)

        image = CachedV2MulledImageMultiTarget(image_name, version_hash, build, identifier)
    else:
        build = None
        if version and "--" in version:
            version, build = split_tag(version)
        if prefix and image_name.startswith(prefix):
            image_name = image_name[len(prefix):]
        image = CachedMulledImageSingleTarget(image_name, version, build, identifier)
    return image


def list_cached_mulled_images_from_path(directory, hash_func="v2"):
    contents = os.listdir(directory)
    raw_images = map(lambda name: identifier_to_cached_target(name, hash_func), contents)
    return [i for i in raw_images if i is not None]


def get_filter(namespace):
    prefix = "quay.io/" if namespace is None else "quay.io/%s" % namespace
    return lambda name: name[0].startswith(prefix) and name[0].count("/") == 2


def find_best_matching_cached_image(targets, cached_images, hash_func):
    if len(targets) == 0:
        return None

    image = None
    if len(targets) == 1:
        target = targets[0]
        for cached_image in cached_images:
            if cached_image.multi_target:
                continue
            if not cached_image.package_name == target.package_name:
                continue
            if not target.version or target.version == cached_image.version:
                image = cached_image
                break
    elif hash_func == "v2":
        name = v2_image_name(targets)
        if ":" in name:
            package_hash, version_hash = name.split(":", 2)
        else:
            package_hash, version_hash = name, None

        for cached_image in cached_images:
            if cached_image.multi_target != "v2":
                continue

            if version_hash is None:
                # Just match on package hash...
                if package_hash == cached_image.package_hash:
                    image = cached_image
                    break
            else:
                # Match on package and version hash...
                if package_hash == cached_image.package_hash and version_hash == cached_image.version_hash:
                    image = cached_image
                    break

    elif hash_func == "v1":
        name = v1_image_name(targets)
        for cached_image in cached_images:
            if cached_image.multi_target != "v1":
                continue

            if name == cached_image.hash:
                image = cached_image
                break
    return image


def docker_cached_container_description(targets, namespace, hash_func="v2", shell=DEFAULT_CONTAINER_SHELL, resolution_cache=None):
    if len(targets) == 0:
        return None

    cached_images = list_docker_cached_mulled_images(namespace, hash_func=hash_func, resolution_cache=resolution_cache)
    image = find_best_matching_cached_image(targets, cached_images, hash_func)

    container = None
    if image:
        container = ContainerDescription(
            image.image_identifier,
            type="docker",
            shell=shell,
        )

    return container


def singularity_cached_container_description(targets, cache_directory, hash_func="v2", shell=DEFAULT_CONTAINER_SHELL):
    if len(targets) == 0:
        return None

    if not os.path.exists(cache_directory):
        return None

    cached_images = list_cached_mulled_images_from_path(cache_directory, hash_func=hash_func)
    image = find_best_matching_cached_image(targets, cached_images, hash_func)

    container = None
    if image:
        container = ContainerDescription(
            os.path.abspath(os.path.join(cache_directory, image.image_identifier)),
            type="singularity",
            shell=shell,
        )

    return container


def targets_to_mulled_name(targets, hash_func, namespace, resolution_cache=None):
    unresolved_cache_key = "galaxy.tool_util.deps.container_resolvers.mulled:unresolved"
    if resolution_cache is not None:
        if unresolved_cache_key not in resolution_cache:
            resolution_cache[unresolved_cache_key] = set()
        unresolved_cache = resolution_cache.get(unresolved_cache_key)
    else:
        unresolved_cache = set()

    mulled_resolution_cache = None
    if resolution_cache and hasattr(resolution_cache, 'mulled_resolution_cache'):
        mulled_resolution_cache = resolution_cache.mulled_resolution_cache

    name = None

    def cached_name(cache_key):
        if mulled_resolution_cache:
            if cache_key in mulled_resolution_cache:
                return resolution_cache.get(cache_key)
        return None

    if len(targets) == 1:
        target = targets[0]
        target_version = target.version
        cache_key = "ns[%s]__single__%s__@__%s" % (namespace, target.package_name, target_version)
        if cache_key in unresolved_cache:
            return None
        name = cached_name(cache_key)
        if name:
            return name

        tags = mulled_tags_for(namespace, target.package_name, resolution_cache=resolution_cache)

        if tags:
            if target_version:
                for tag in tags:
                    if '--' in tag:
                        version, build = split_tag(tag)
                    else:
                        version = tag
                        build = None
                    if version == target_version:
                        name = "%s:%s" % (target.package_name, version)
                        if build:
                            name = "%s--%s" % (name, build)
                        break
            else:
                version, build = split_tag(tags[0])
                name = "%s:%s--%s" % (target.package_name, version, build)

    else:
        def first_tag_if_available(image_name):
            if ":" in image_name:
                repo_name, tag_prefix = image_name.split(":", 2)
            else:
                repo_name = image_name
                tag_prefix = None
            tags = mulled_tags_for(namespace, repo_name, tag_prefix=tag_prefix, resolution_cache=resolution_cache)
            return tags[0] if tags else None

        if hash_func == "v2":
            base_image_name = v2_image_name(targets)
        elif hash_func == "v1":
            base_image_name = v1_image_name(targets)
        else:
            raise Exception("Unimplemented mulled hash_func [%s]" % hash_func)

        cache_key = "ns[%s]__%s__%s" % (namespace, hash_func, base_image_name)
        if cache_key in unresolved_cache:
            return None
        name = cached_name(cache_key)
        if name:
            return name

        tag = first_tag_if_available(base_image_name)
        if tag:
            if ":" in base_image_name:
                assert hash_func != "v1"
                # base_image_name of form <package_hash>:<version_hash>, expand tag
                # to include build number in tag.
                name = "%s:%s" % (base_image_name.split(":")[0], tag)
            else:
                # base_image_name of form <package_hash>, simply add build number
                # as tag to fully qualify image.
                name = "%s:%s" % (base_image_name, tag)

    if name and mulled_resolution_cache:
        mulled_resolution_cache.put(cache_key, name)

    if name is None:
        unresolved_cache.add(name)

    return name


@six.python_2_unicode_compatible
class CachedMulledDockerContainerResolver(ContainerResolver):

    resolver_type = "cached_mulled"
    container_type = "docker"
    shell = '/bin/bash'

    def __init__(self, app_info=None, namespace="biocontainers", hash_func="v2", **kwds):
        super(CachedMulledDockerContainerResolver, self).__init__(app_info)
        self.namespace = namespace
        self.hash_func = hash_func

    def resolve(self, enabled_container_types, tool_info, **kwds):
        if tool_info.requires_galaxy_python_environment or self.container_type not in enabled_container_types:
            return None

        targets = mulled_targets(tool_info)
        resolution_cache = kwds.get("resolution_cache")
        return docker_cached_container_description(targets, self.namespace, hash_func=self.hash_func, shell=self.shell, resolution_cache=resolution_cache)

    def __str__(self):
        return "CachedMulledDockerContainerResolver[namespace=%s]" % self.namespace


@six.python_2_unicode_compatible
class CachedMulledSingularityContainerResolver(ContainerResolver):

    resolver_type = "cached_mulled_singularity"
    container_type = "singularity"
    shell = '/bin/bash'

    def __init__(self, app_info=None, hash_func="v2", **kwds):
        super(CachedMulledSingularityContainerResolver, self).__init__(app_info)
        self.cache_directory = kwds.get("cache_directory", os.path.join(app_info.container_image_cache_path, "singularity", "mulled"))
        self.hash_func = hash_func

    def resolve(self, enabled_container_types, tool_info, **kwds):
        if tool_info.requires_galaxy_python_environment or self.container_type not in enabled_container_types:
            return None

        targets = mulled_targets(tool_info)
        return singularity_cached_container_description(targets, self.cache_directory, hash_func=self.hash_func, shell=self.shell)

    def __str__(self):
        return "CachedMulledSingularityContainerResolver[cache_directory=%s]" % self.cache_directory


@six.python_2_unicode_compatible
class MulledDockerContainerResolver(ContainerResolver):
    """Look for mulled images matching tool dependencies."""

    resolver_type = "mulled"
    container_type = "docker"
    shell = '/bin/bash'
    protocol = None

    def __init__(self, app_info=None, namespace="biocontainers", hash_func="v2", auto_install=True, **kwds):
        super(MulledDockerContainerResolver, self).__init__(app_info)
        self.namespace = namespace
        self.hash_func = hash_func
        self.auto_install = string_as_bool(auto_install)

    def cached_container_description(self, targets, namespace, hash_func, resolution_cache):
        return docker_cached_container_description(targets, namespace, hash_func, resolution_cache)

    def pull(self, container):
        command = container.build_pull_command()
        shell(command)

    def resolve(self, enabled_container_types, tool_info, install=False, **kwds):
        resolution_cache = kwds.get("resolution_cache")
        if tool_info.requires_galaxy_python_environment or self.container_type not in enabled_container_types:
            return None

        targets = mulled_targets(tool_info)
        if len(targets) == 0:
            return None

        name = targets_to_mulled_name(targets=targets, hash_func=self.hash_func, namespace=self.namespace, resolution_cache=resolution_cache)
        if name:
            container_id = "quay.io/%s/%s" % (self.namespace, name)
            if self.protocol:
                container_id = "%s%s" % (self.protocol, container_id)
            container_description = ContainerDescription(
                container_id,
                type=self.container_type,
                shell=self.shell,
            )
            destination_for_container_type = kwds.get('destination_for_container_type')
            if install and destination_for_container_type and not self.cached_container_description(
                    targets,
                    namespace=self.namespace,
                    hash_func=self.hash_func,
                    resolution_cache=resolution_cache,
            ):
                container = CONTAINER_CLASSES[self.container_type](container_description.identifier,
                                                                   self.app_info,
                                                                   tool_info,
                                                                   destination_for_container_type(self.container_type),
                                                                   {},
                                                                   container_description)
                self.pull(container)
            if not self.auto_install:
                container_description = self.cached_container_description(
                    targets,
                    namespace=self.namespace,
                    hash_func=self.hash_func,
                    resolution_cache=resolution_cache,
                )
            return container_description

    def __str__(self):
        return "MulledDockerContainerResolver[namespace=%s]" % self.namespace


@six.python_2_unicode_compatible
class MulledSingularityContainerResolver(MulledDockerContainerResolver):

    resolver_type = "mulled_singularity"
    container_type = "singularity"
    protocol = 'docker://'

    def __init__(self, app_info=None, namespace="biocontainers", hash_func="v2", auto_install=True, **kwds):
        super(MulledSingularityContainerResolver, self).__init__(app_info)
        self.cache_directory = kwds.get("cache_directory", os.path.join(app_info.container_image_cache_path, "singularity", "mulled"))
        self.namespace = namespace
        self.hash_func = hash_func
        self.auto_install = string_as_bool(auto_install)

    def cached_container_description(self, targets, namespace, hash_func):
        return singularity_cached_container_description(targets,
                                                        cache_directory=self.cache_directory,
                                                        hash_func=hash_func)

    def pull(self, container):
        cmds = container.build_mulled_singularity_pull_command(cache_directory=self.cache_directory, namespace=self.namespace)
        shell(cmds=cmds)

    def __str__(self):
        return "MulledSingularityContainerResolver[namespace=%s]" % self.namespace


@six.python_2_unicode_compatible
class BuildMulledDockerContainerResolver(ContainerResolver):
    """Build for Docker mulled images matching tool dependencies."""

    resolver_type = "build_mulled"
    container_type = "docker"
    shell = '/bin/bash'
    builds_on_resolution = True

    def __init__(self, app_info=None, namespace="local", hash_func="v2", auto_install=True, **kwds):
        super(BuildMulledDockerContainerResolver, self).__init__(app_info)
        self._involucro_context_kwds = {
            'involucro_bin': self._get_config_option("involucro_path", None)
        }
        self.namespace = namespace
        self.hash_func = hash_func
        self.auto_install = string_as_bool(auto_install)
        self._mulled_kwds = {
            'namespace': namespace,
            'channels': self._get_config_option("mulled_channels", DEFAULT_CHANNELS),
            'hash_func': self.hash_func,
            'command': 'build-and-test',
        }
        self.auto_init = self._get_config_option("involucro_auto_init", True)

    def resolve(self, enabled_container_types, tool_info, install=False, **kwds):
        if tool_info.requires_galaxy_python_environment or self.container_type not in enabled_container_types:
            return None

        targets = mulled_targets(tool_info)
        if len(targets) == 0:
            return None
        if self.auto_install or install:
            mull_targets(
                targets,
                involucro_context=self._get_involucro_context(),
                **self._mulled_kwds
            )
        return docker_cached_container_description(targets, self.namespace, hash_func=self.hash_func, shell=self.shell)

    def _get_involucro_context(self):
        involucro_context = InvolucroContext(**self._involucro_context_kwds)
        self.enabled = ensure_installed(involucro_context, self.auto_init)
        return involucro_context

    def __str__(self):
        return "BuildDockerContainerResolver[namespace=%s]" % self.namespace


@six.python_2_unicode_compatible
class BuildMulledSingularityContainerResolver(ContainerResolver):
    """Build for Singularity mulled images matching tool dependencies."""

    resolver_type = "build_mulled_singularity"
    container_type = "singularity"
    shell = '/bin/bash'
    builds_on_resolution = True

    def __init__(self, app_info=None, hash_func="v2", auto_install=True, **kwds):
        super(BuildMulledSingularityContainerResolver, self).__init__(app_info)
        self._involucro_context_kwds = {
            'involucro_bin': self._get_config_option("involucro_path", None)
        }
        self.cache_directory = kwds.get("cache_directory", os.path.join(app_info.container_image_cache_path, "singularity", "mulled"))
        self.hash_func = hash_func
        self.auto_install = string_as_bool(auto_install)
        self._mulled_kwds = {
            'channels': self._get_config_option("mulled_channels", DEFAULT_CHANNELS),
            'hash_func': self.hash_func,
            'command': 'build-and-test',
            'singularity': True,
            'singularity_image_dir': self.cache_directory,
        }
        self.auto_init = self._get_config_option("involucro_auto_init", True)

    def resolve(self, enabled_container_types, tool_info, install=False, **kwds):
        if tool_info.requires_galaxy_python_environment or self.container_type not in enabled_container_types:
            return None

        targets = mulled_targets(tool_info)
        if len(targets) == 0:
            return None

        if self.auto_install or install:
            mull_targets(
                targets,
                involucro_context=self._get_involucro_context(),
                **self._mulled_kwds
            )
        return singularity_cached_container_description(targets, self.cache_directory, hash_func=self.hash_func, shell=self.shell)

    def _get_involucro_context(self):
        involucro_context = InvolucroContext(**self._involucro_context_kwds)
        self.enabled = ensure_installed(involucro_context, self.auto_init)
        return involucro_context

    def __str__(self):
        return "BuildSingularityContainerResolver[cache_directory=%s]" % self.cache_directory


def mulled_targets(tool_info):
    return requirements_to_mulled_targets(tool_info.requirements)


__all__ = (
    "CachedMulledDockerContainerResolver",
    "CachedMulledSingularityContainerResolver",
    "MulledDockerContainerResolver",
    "MulledSingularityContainerResolver",
    "BuildMulledDockerContainerResolver",
    "BuildMulledSingularityContainerResolver",
)
