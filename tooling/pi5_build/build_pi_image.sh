#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build the Pi 5 test-host image via pi-gen + our custom stage.
# Assumes build_pi_kernel.sh has already produced kernel artifacts.
# Per D022-D034 (especially D023, D028-D030, D033).
#
# Run as regular user; uses sudo internally when pi-gen needs it.

set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "ERROR: run as your regular user (not root). pi-gen invokes sudo itself." >&2
    exit 1
fi

# Canonicalize so the later case-guard against WORK_DIR_ABS (which IS
# realpath-ed) doesn't spuriously reject builds when the repo path
# contains a symlink component.
REPO_ROOT="$(realpath -m "$(cd "$(dirname "$0")/../.." && pwd)")"
cd "$REPO_ROOT"

PI_GEN_DIR="${PI_GEN_DIR:-build/pi-gen}"
KERNEL_OUT="${KERNEL_OUT:-build/pi-kernel}"
STAGE_DIR="${STAGE_DIR:-tooling/pi5_build/pi_gen_stage}"
IMAGE_OUT="${IMAGE_OUT:-build/pi-image}"
SSH_KEY_PRIV="${SSH_KEY_PRIV:-$HOME/.ssh/fireasm_pi5_ed}"
SSH_KEY_PUB="${SSH_KEY_PUB:-${SSH_KEY_PRIV}.pub}"

### Prerequisites ###################################################
check() {
    local p="$1" msg="$2"
    [[ -e "$p" ]] || { echo "ERROR: $msg ($p)" >&2; exit 1; }
}

check "$PI_GEN_DIR/build.sh"                                 "pi-gen not cloned"
check "$KERNEL_OUT/kernel_2712.img"                          "kernel not built (run build_pi_kernel.sh first)"
check "$KERNEL_OUT/dtb/bcm2712-rpi-5-b.dtb"                  "kernel DTB missing"
check "$KERNEL_OUT/modules_staging/lib/modules"              "kernel modules missing"
check "$STAGE_DIR/EXPORT_IMAGE"                              "custom stage dir missing"

### SSH keypair generation ##########################################
if [[ ! -f "$SSH_KEY_PRIV" ]]; then
    echo "--- generating SSH keypair for Pi 5 access ---"
    install -d -m 0700 "$(dirname "$SSH_KEY_PRIV")"
    ssh-keygen -t ed25519 -N '' \
        -C "fireasm-pi5-$(whoami)@$(hostname)-$(date +%Y%m%d)" \
        -f "$SSH_KEY_PRIV"
    chmod 0600 "$SSH_KEY_PRIV"
    chmod 0644 "$SSH_KEY_PUB"
fi

### Make stage scripts executable (idempotent) ######################
find "$STAGE_DIR" -type f -name '*.sh' -exec chmod +x {} \;

### Absolute paths for pi-gen #######################################
KERNEL_OUT_ABS="$REPO_ROOT/$KERNEL_OUT"
STAGE_DIR_ABS="$REPO_ROOT/$STAGE_DIR"
IMAGE_OUT_ABS="$REPO_ROOT/$IMAGE_OUT"
SSH_KEY_PUB_ABS="$(readlink -f "$SSH_KEY_PUB")"
# pi-gen sources its config as a shell fragment. Escape the pubkey path
# so an exotic $HOME (with spaces, quotes, or other shell metacharacters)
# can't produce malformed config when this path is interpolated later.
SSH_KEY_PUB_ABS_ESC="$(printf '%q' "$SSH_KEY_PUB_ABS")"

mkdir -p "$IMAGE_OUT_ABS"

### Random per-build FIRST_USER_PASS ################################
# Even with PUBKEY_ONLY_SSH=1, a hardcoded password is a shared secret in
# every image — weak against console access and SSH-config drift. Generate
# a fresh alphanumeric password per build. 32 chars of [A-Za-z0-9] is
# comfortably above brute-force territory; alnum-only keeps shell quoting
# in the pi-gen config trivially safe.
#
# 'cut -c1-32' (rather than 'head -c 32') reads the full pipeline and
# avoids SIGPIPE-under-pipefail flakiness on early pipe closure.
#
# The password is written to a 0600 sidecar next to the image, NOT echoed
# to stderr — we don't want it leaking into any build log if the script's
# output gets captured. Override with FIRST_USER_PASS_OVERRIDE only for
# deterministic reproducibility tests.
RANDOM_FIRST_USER_PASS="${FIRST_USER_PASS_OVERRIDE:-$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-32)}"
# Defense-in-depth: 48 base64 bytes yield ~64 chars, of which ~62 are alnum,
# so truncating to 32 should always succeed. Assert anyway — a silent short
# password would weaken the image with no signal.
if [[ ${#RANDOM_FIRST_USER_PASS} -ne 32 ]]; then
    echo "ERROR: generated password has unexpected length ${#RANDOM_FIRST_USER_PASS} (expected 32)." >&2
    exit 1
fi
PW_SIDECAR="$IMAGE_OUT_ABS/first-user-pass.secret"
install -m 0600 /dev/null "$PW_SIDECAR"
printf '%s\n' "$RANDOM_FIRST_USER_PASS" > "$PW_SIDECAR"
echo "--- first-user password written to $PW_SIDECAR (mode 0600, console-fallback only)"

### Pre-flight: nuke stale pi-gen work dir ##########################
# Stage0's prerun.sh skips debootstrap if ${ROOTFS_DIR} exists, which
# poisons retries after a mid-stage failure. Safer to always start fresh.
#
# Guard: PI_GEN_DIR is env-overridable, and the resulting path feeds a
# sudo rm -rf. Verify the resolved path (a) lives under $REPO_ROOT and
# (b) ends with /work, so an accidental PI_GEN_DIR=../.. or =/ can't
# take out unrelated trees.
WORK_DIR_ABS="$(realpath -m "$REPO_ROOT/$PI_GEN_DIR/work")"
case "$WORK_DIR_ABS/" in
    "$REPO_ROOT"/*) : ;;
    *)
        echo "ERROR: PI_GEN_DIR='$PI_GEN_DIR' resolves to '$WORK_DIR_ABS', outside '$REPO_ROOT'. Refusing." >&2
        exit 1
        ;;
esac
case "$WORK_DIR_ABS" in
    */work) : ;;
    *)
        echo "ERROR: resolved WORK_DIR_ABS='$WORK_DIR_ABS' does not end in /work. Refusing." >&2
        exit 1
        ;;
esac
if [[ -d "$WORK_DIR_ABS" ]]; then
    echo "--- pre-flight: removing stale pi-gen work dir ---"
    sudo rm -rf "$WORK_DIR_ABS"
fi

### Skip pi-gen stages / sub-stages we don't want ###################
# stage3+ are desktop flavors.
for stage in stage3 stage4 stage5; do
    touch "$PI_GEN_DIR/$stage/SKIP"
    touch "$PI_GEN_DIR/$stage/SKIP_IMAGES"
done
# ENABLE_CLOUD_INIT=0 in config gates 04-cloud-init/01-run.sh but NOT
# 00-packages, so cloud-init still gets installed. Skip the sub-stage
# entirely.
touch "$PI_GEN_DIR/stage2/04-cloud-init/SKIP"

### Write pi-gen config #############################################
# PUBKEY_SSH_FIRST_USER holds the pubkey *content* (not a path). pi-gen
# sources this config as a shell fragment, so $(cat ...) is evaluated
# at source time and we get the key contents in-line.
#
# Threat model for the produced image (combining PUBKEY_ONLY_SSH=1 and
# PASSWORDLESS_SUDO=1 below):
#   - Anyone holding the private key at ~/.ssh/fireasm_pi5_ed on the
#     building laptop gets root on the Pi. No second factor.
#   - This is acceptable for a headless test host on the D024 isolated
#     laptop↔Pi bridge (no NAT, no internet route, not addressable from
#     the LAN) used for local dev by one operator.
#   - Do NOT reuse this image design for anything internet-facing or
#     multi-operator without hardening sudo (password, MFA, per-cmd rules)
#     and rotating the SSH key from a trusted bootstrap path.
CONFIG_FILE="$PI_GEN_DIR/config"
cat > "$CONFIG_FILE" <<EOF
IMG_NAME='fireasm-test'
PI_GEN_RELEASE='fireasmserver Pi 5 test image'
RELEASE='trixie'
DEPLOY_COMPRESSION=none
DEPLOY_DIR='${IMAGE_OUT_ABS}'
STAGE_LIST='stage0 stage1 stage2 ${STAGE_DIR_ABS}'
ENABLE_CLOUD_INIT=0
LOCALE_DEFAULT='en_US.UTF-8'
KEYBOARD_KEYMAP='us'
KEYBOARD_LAYOUT='English (US)'
TIMEZONE_DEFAULT='America/Los_Angeles'
TARGET_HOSTNAME='fireasm-test'
FIRST_USER_NAME='ed'
FIRST_USER_PASS='${RANDOM_FIRST_USER_PASS}'
DISABLE_FIRST_BOOT_USER_RENAME=1
ENABLE_SSH=1
PUBKEY_ONLY_SSH=1
PUBKEY_SSH_FIRST_USER="\$(cat ${SSH_KEY_PUB_ABS_ESC})"
PASSWORDLESS_SUDO=1
EOF

echo "--- pi-gen config (FIRST_USER_PASS redacted) ---"
# Don't print the cleartext password; it lives in $PW_SIDECAR at 0600.
sed "s|^FIRST_USER_PASS=.*|FIRST_USER_PASS='<redacted — see $PW_SIDECAR>'|" "$CONFIG_FILE"
echo

### Run pi-gen ######################################################
echo "--- running pi-gen (this takes 30-60 minutes on first run) ---"
echo "--- exports: FIREASMSERVER_KERNEL_DIR=$KERNEL_OUT_ABS"
echo

export FIREASMSERVER_KERNEL_DIR="$KERNEL_OUT_ABS"
# D035: forward proxy config overrides to pi_gen_stage/06-apt-proxy so a
# user overriding these at build time sees them baked into the image.
# Also forwarded to pi_setup.sh by virtue of the same env var names.
export FIREASMSERVER_LAPTOP_IP="${FIREASMSERVER_LAPTOP_IP:-10.0.0.1}"
export FIREASMSERVER_PROXY_PORT="${FIREASMSERVER_PROXY_PORT:-3142}"
# Force noninteractive for every apt/dpkg invocation inside every chroot.
# A dpkg prompt (dialog/whiptail) with no tty attached will hang the build
# indefinitely — which is what happened on the D034 run.
export DEBIAN_FRONTEND=noninteractive
export DEBCONF_NONINTERACTIVE_SEEN=true
export NEEDRESTART_MODE=a

BUILD_LOG="$IMAGE_OUT_ABS/pi-gen-build.log"
echo "--- build log: $BUILD_LOG ---"

# Prime sudo credential timestamp so the piped invocation below doesn't
# need to prompt (it has no controlling tty through the pipe).
sudo -v

cd "$PI_GEN_DIR"
sudo -E ./build.sh </dev/null 2>&1 | tee "$BUILD_LOG"

### Summary #########################################################
echo
echo "=== pi-gen build complete ==="
echo "Output directory: $IMAGE_OUT_ABS"
ls -la "$IMAGE_OUT_ABS"
echo
echo "To flash (replace /dev/sdX with the target SD card device):"
echo "  ./tooling/pi5_build/flash_sd_card.sh /dev/sdX"
echo "  # or use 'rpi-imager' GUI for a point-and-click flow"
echo
echo "SSH access after first boot:"
echo "  ssh -i $SSH_KEY_PRIV ed@10.0.0.2"
