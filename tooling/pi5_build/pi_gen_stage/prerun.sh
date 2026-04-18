#!/bin/bash -e
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copy the previous stage's rootfs into this stage's work dir.
# Standard pi-gen prerun pattern.

if [ ! -d "${ROOTFS_DIR}" ]; then
    copy_previous
fi
