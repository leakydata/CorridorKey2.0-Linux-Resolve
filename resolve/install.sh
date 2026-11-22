#!/usr/bin/env bash
# One-command installer: CorridorKey for Linux + DaVinci Resolve Studio.
#
#   curl -fsSL https://raw.githubusercontent.com/leakydata/CorridorKey2.0-Linux-Resolve/linux-resolve/resolve/install.sh | bash
#
# or, from a clone:  resolve/install.sh
#
# What it does (safe to re-run; it resumes and updates):
#   1. checks the machine (NVIDIA GPU, ffmpeg, git, Resolve) and tells you what is missing
#   2. gets the code (clone, or pull if already there)
#   3. installs `uv` (a small Python manager) if you don't have it, then builds a private Python 3.12 environment
#      with PyTorch (CUDA 12.8) inside the install folder - nothing is installed system-wide, no sudo needed
#   4. downloads the model weights (~5 GB) and the person-matte model (~233 MB)
#   5. adds the DaVinci Resolve menu entries and the `corridorkey-resolve` command
#
# Options:  --dir DIR   install folder (default ~/.local/share/corridorkey-resolve; a clone you run it from is used as is)
#           --yes       don't ask for confirmation
#           --no-hint-model   skip the person-matte model download
# Environment: CK_REPO / CK_BRANCH override the repository and branch to install from.
set -euo pipefail

REPO="${CK_REPO:-https://github.com/leakydata/CorridorKey2.0-Linux-Resolve.git}"
BRANCH="${CK_BRANCH:-linux-resolve}"
DIR=""
YES=0
HINT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --no-hint-model) HINT=0; shift ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m[warn]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
ask() {  # ask "question" -> 0 for yes; reads the terminal even when piped from curl
  [ "$YES" = 1 ] && return 0
  local a=""
  if [ -r /dev/tty ]; then read -r -p "$1 [Y/n] " a < /dev/tty || true; else return 0; fi
  case "$a" in n|N|no|NO) return 1 ;; *) return 0 ;; esac
}

# --- where to install: the clone this script lives in, else --dir, else the default
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$DIR" ] && [ -n "$HERE" ] && [ -f "$HERE/../infer.py" ] && [ -f "$HERE/corridorkey_resolve.py" ]; then
  DIR="$(cd "$HERE/.." && pwd)"
fi
DIR="${DIR:-$HOME/.local/share/corridorkey-resolve}"

say "Checking this computer"
[ "$(uname -s)" = Linux ] || die "this installer is for Linux only"
[ "$(uname -m)" = x86_64 ] || die "x86_64 only (PyTorch CUDA builds); found $(uname -m)"
MISSING=()
command -v git >/dev/null || MISSING+=(git)
command -v curl >/dev/null || MISSING+=(curl)
command -v ffmpeg >/dev/null || MISSING+=(ffmpeg)
command -v ffprobe >/dev/null || MISSING+=(ffmpeg)
if [ ${#MISSING[@]} -gt 0 ]; then
  die "missing: $(printf '%s ' "${MISSING[@]}")- install with your package manager (Ubuntu/Debian/Kubuntu: sudo apt install git curl ffmpeg), then run this again"
fi
command -v nvidia-smi >/dev/null || die "nvidia-smi not found: the NVIDIA driver is required (CorridorKey runs on an NVIDIA GPU)"
GOOD=$(nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader,nounits | awk -F', ' '$2+0 >= 8.0 {print $1" ("$3" MB)"}')
if [ -n "$GOOD" ]; then
  echo "GPU ok: $(echo "$GOOD" | head -1)"
else
  die "no NVIDIA GPU with compute capability 8.0+ found (RTX 30-series or newer is required). Found: $(nvidia-smi --query-gpu=name --format=csv,noheader | paste -sd,)"
fi
[ -x /opt/resolve/bin/resolve ] || warn "DaVinci Resolve not found in /opt/resolve - install Resolve Studio before using the menu entries"
command -v notify-send >/dev/null || warn "notify-send not found: job notifications will not show (sudo apt install libnotify-bin)"
FREE=$(df -Pk "$(dirname "$DIR")" 2>/dev/null | awk 'NR==2 {print int($4/1048576)}' || echo 0)
[ "${FREE:-0}" -ge 15 ] || warn "only ${FREE:-?} GB free where it installs; about 15 GB are needed"

say "What will be installed"
cat <<EOF
  folder:   $DIR   (code + a private Python environment + models, ~12 GB)
  commands: ~/.local/bin/corridorkey-resolve, and 3 entries under Resolve > Workspace > Scripts > Utility
  downloads: PyTorch (CUDA), CorridorKey 2.0 weights, and the foundation models NVIDIA C-RADIOv4 (NVIDIA Open
             Model License), Microsoft MoGe-2 (MIT), Google DeepMind RVM (CC-BY 4.0)$([ "$HINT" = 1 ] && echo ", torchvision DeepLabV3 (person matte)")
  licence:  CorridorKey is under the Corridor Key Licence 2.0 (CC BY-NC-SA 4.0 + extra terms): free to use, no resale,
            companies over US\$20M revenue need the official plugin. See LICENSE and THIRD_PARTY_NOTICES.md.
  This is an unofficial community integration, not the paid plugin from corridorkey.com.
EOF
ask "Continue?" || { echo "cancelled"; exit 1; }

say "Getting the code"
if [ -f "$DIR/infer.py" ] && [ -f "$DIR/resolve/corridorkey_resolve.py" ]; then
  if [ -d "$DIR/.git" ] && [ -z "$(git -C "$DIR" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    git -C "$DIR" pull --ff-only 2>&1 | tail -1 || warn "could not update; continuing with the code that is there"
  else
    echo "using the code already in $DIR"
  fi
else
  mkdir -p "$(dirname "$DIR")"
  git clone --branch "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

say "Python environment (private, in $DIR/venv)"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  echo "installing uv (Astral's Python manager) into ~/.local/bin ..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || die "uv install failed"
fi
[ -x venv/bin/python ] || uv venv -p 3.12 venv
export VIRTUAL_ENV="$DIR/venv"
if ! venv/bin/python -c "import torch, torchvision" 2>/dev/null; then
  echo "installing PyTorch (about 3 GB) ..."
  uv pip install torch==2.9.1 torchvision==0.24.1 --index-url https://download.pytorch.org/whl/cu128
fi
echo "installing requirements ..."
uv pip install -r requirements.txt

say "Model weights (about 5 GB; skips what is already there)"
venv/bin/python scripts/download_weights.py

say "DaVinci Resolve menu entries and command"
INSTALL_ARGS=()
[ "$HINT" = 1 ] || INSTALL_ARGS+=(--no-hint-model)
python3 resolve/install.py "${INSTALL_ARGS[@]}"

say "Checking the install"
"$DIR/venv/bin/python" resolve/ck.py doctor 2>/dev/null | grep -v "Resolve scripting\|project open\|Resolve Studio" || true

cat <<EOF

Done. Next:
  1. In DaVinci Resolve Studio: Preferences > System > General > "External scripting using: Local", then restart Resolve.
  2. Select a clip on the timeline: Workspace > Scripts > Utility > CorridorKey - Key selected clip
     (a green/blue screen) or "CorridorKey - Key person (no green screen)".
  3. Progress shows as a desktop notification. Check everything any time with:  corridorkey-resolve doctor
  Update later:  corridorkey-resolve update      Remove:  python3 $DIR/resolve/install.py --uninstall
EOF
