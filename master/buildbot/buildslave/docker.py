# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

# This module is left for backward compatibility of old-named worker API.
# It should never be imported by Buildbot.

from buildbot.worker_transition import define_old_worker_class
from buildbot.worker_transition import on_deprecated_module_usage

on_deprecated_module_usage(
    "'{old}' module is deprecated, use "
    "'buildbot.worker.docker' module instead".format(old=__name__))

from buildbot.worker.docker import DockerLatentWorker as _DockerLatentWorker
# TODO: Is this private function?
from buildbot.worker.docker import handle_stream_line

define_old_worker_class(locals(), _DockerLatentWorker, pattern="BuildWorker")

__all__ = ("DockerLatentBuildSlave", "handle_stream_line")
