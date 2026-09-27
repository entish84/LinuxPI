#!/usr/bin/env python3
"""
Universal Cross-Distro Post-Installation & Developer Setup
Supports: Debian/Ubuntu, Fedora/RHEL, Arch Linux

Architecture:
- Universal Core (Shared): Dotfiles, Zinit, Nerd Fonts, Rider, Android Studio, Mise, SSH
- Distro Adapters: Debian/APT, Fedora/DNF, Arch/Pacman
"""

import os
import sys
import shutil
import subprocess
import urllib.request
import urllib.error
import json
import re
import pwd
import tempfile
import argparse
import time
from pathlib import Path
from abc import ABC, abstractmethod

# ==============================================================================
# Terminal UI & Formatting
# ==============================================================================
class Colors:
    BLUE = "\033[1;34m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    RED = "\033[1;31m"
    CYAN = "\033[1;36m"
    MAGENTA = "\033[1;35m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

def log_info(msg):
    print(f"{Colors.BLUE}[INFO]{Colors.RESET} {msg}")

def log_success(msg):
    print(f"{Colors.GREEN}[SUCCESS]{Colors.RESET} {msg}")

def log_warn(msg):
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {msg}")

def log_error(msg):
    print(f"{Colors.RED}[ERROR]{Colors.RESET} {msg}", file=sys.stderr)

def log_section(title):
    print(f"\n{Colors.CYAN}{'=' * 68}")
    print(f" {title}")
    print(f"{'=' * 68}{Colors.RESET}")

# ==============================================================================
# Helper Utilities
# ==============================================================================
def run_cmd(cmd, check=True, capture=False, as_user=None, env=None):
    """Executes a command directly or under the context of a target user."""
    if as_user and as_user != "root":
        if isinstance(cmd, list):
            import shlex
            cmd_str = " ".join(shlex.quote(c) for c in cmd)
        else:
            cmd_str = cmd
        full_cmd = ["sudo", "-u", as_user, "-H", "bash", "-c", cmd_str]
        shell = False
    else:
        full_cmd = cmd
        shell = isinstance(cmd, str)

    try:
        res = subprocess.run(
            full_cmd,
            shell=shell,
            check=check,
            capture_output=capture,
            text=True,
            env=env
        )
        return res
    except subprocess.CalledProcessError as e:
        log_warn(f"Command failed (exit {e.returncode}): {cmd}")
        if check:
            raise
        return e

def download_file(url, dest_path, headers=None):
    """Downloads a file from a URL to a local path with error handling."""
    req_headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)

def extract_deb_payload(deb_path, extract_root="/"):
    """
    Directly extracts a Debian package data tarball into extract_root.
    Parses Unix ar archive headers in pure Python to eliminate external ar syntax issues.
    """
    with open(deb_path, "rb") as f:
        magic = f.read(8)
        if magic != b"!<arch>\n":
            raise ValueError(f"Invalid deb archive header: {magic}")

        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                break

            name = hdr[:16].decode("ascii", errors="ignore").strip().rstrip("/")
            size_str = hdr[48:58].decode("ascii", errors="ignore").strip()
            if not size_str.isdigit():
                break
            size = int(size_str)

            if name.startswith("data.tar"):
                suffix = name[name.find(".tar"):]
                data = f.read(size)
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(data)
                    tar_tmp = tf.name
                try:
                    run_cmd(["tar", "-xf", tar_tmp, "-C", extract_root])
                finally:
                    if os.path.exists(tar_tmp):
                        os.remove(tar_tmp)
                return True
            else:
                f.seek(size, os.SEEK_CUR)

            if size % 2 == 1:
                f.seek(1, os.SEEK_CUR)
    return False

def prompt_yn(prompt_msg, default_no=True, non_interactive=False):
    """Interactive Y/N prompt with fallback for non-interactive environments."""
    default_str = "y/N" if default_no else "Y/n"
    full_prompt = f"{prompt_msg} [{default_str}]: "
    default_ans = False if default_no else True

    if non_interactive or not sys.stdin.isatty():
        choice_str = "n" if default_no else "y"
        print(f"{full_prompt}(defaulting to {choice_str})")
        return default_ans

    try:
        ans = input(full_prompt).strip().lower()
        if not ans:
            return default_ans
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return default_ans

def get_target_user():
    """Detects the real non-root user running the script via sudo."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return sudo_user
    try:
        proc = subprocess.run(["logname"], capture_output=True, text=True, check=False)
        name = proc.stdout.strip()
        if name and name != "root":
            return name
    except Exception:
        pass
    try:
        for p in pwd.getpwall():
            if 1000 <= p.pw_uid < 60000:
                return p.pw_name
    except Exception:
        pass
    return "root"

def detect_distro_family():
    """Reads /etc/os-release to detect the Linux distribution family."""
    info = {}
    if os.path.exists("/etc/os-release"):
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    info[k] = v.strip("\"'")

    distro_id = info.get("ID", "").lower()
    id_like = info.get("ID_LIKE", "").lower().split()

    if distro_id in ("debian", "ubuntu", "linuxmint", "pop", "kali", "devuan") or "debian" in id_like or "ubuntu" in id_like:
        return "debian", info
    elif distro_id in ("fedora", "rhel", "almalinux", "rocky", "centos") or "fedora" in id_like or "rhel" in id_like:
        return "fedora", info
    elif distro_id in ("arch", "endeavouros", "manjaro", "garuda") or "arch" in id_like:
        return "arch", info
    else:
        return "unknown", info

# ==============================================================================
# Abstract Distro Adapter
# ==============================================================================
class BaseDistro(ABC):
    def __init__(self, target_user, target_home, os_info):
        self.target_user = target_user
        self.target_home = target_home
        self.os_info = os_info
        self.arch = run_cmd(["uname", "-m"], capture=True).stdout.strip()

    @abstractmethod
    def clean_legacy(self):
        """Removes stale/conflicting repositories and packages."""
        pass

    @abstractmethod
    def configure_package_manager(self, config):
        """Configures mirrors, package manager settings, and third-party repos."""
        pass

    @abstractmethod
    def system_upgrade(self):
        """Runs a distribution base package upgrade."""
        pass

    @abstractmethod
    def install_system_packages(self, install_desktop_apps):
        """Installs compilers, core CLI utilities, fonts, and desktop apps."""
        pass

    @abstractmethod
    def install_vscode(self):
        """Installs Visual Studio Code."""
        pass

    @abstractmethod
    def install_antigravity(self):
        """Installs Google Antigravity IDE."""
        pass

    @abstractmethod
    def post_system_setup(self):
        """Configures systemd services and user group memberships."""
        pass

# ==============================================================================
# Debian / Ubuntu Distro Adapter
# ==============================================================================
class DebianDistro(BaseDistro):
    def clean_legacy(self):
        log_info("Cleaning legacy packages and obsolete repository lists...")
        for pattern in ["/etc/apt/sources.list.d/docker*.list",
                        "/etc/apt/sources.list.d/mise*.list",
                        "/etc/apt/sources.list.d/gierens*.list"]:
            run_cmd(f"rm -f {pattern}", check=False)
        for pkg in ["docker.io", "docker-doc", "docker-compose", "podman-docker", "containerd", "runc"]:
            run_cmd(f"dpkg -s {pkg} >/dev/null 2>&1 && apt-get remove -y {pkg} || true", check=False)

    def configure_package_manager(self, config):
        log_info("Configuring APT repositories and keyrings...")
        os.environ["DEBIAN_FRONTEND"] = "noninteractive"

        apt_conf = Path("/etc/apt/apt.conf.d/99post-install")
        apt_conf.write_text('Acquire::Languages "none";\nAcquire::Retries "3";\nAPT::Color "1";\nDpkg::Progress-Fancy "1";\n')

        deb_sources = Path("/etc/apt/sources.list.d/debian.sources")
        if deb_sources.exists():
            content = deb_sources.read_text()
            content = re.sub(r"^Components: main(\s*)$", r"Components: main contrib non-free non-free-firmware\1", content, flags=re.MULTILINE)
            deb_sources.write_text(content)
        classic_sources = Path("/etc/apt/sources.list")
        if classic_sources.exists():
            content = classic_sources.read_text()
            content = re.sub(r"^(deb .* main)$", r"\1 contrib non-free non-free-firmware", content, flags=re.MULTILINE)
            classic_sources.write_text(content)

        if shutil.which("add-apt-repository"):
            run_cmd("add-apt-repository -y universe 2>/dev/null || true", check=False)
            run_cmd("add-apt-repository -y multiverse 2>/dev/null || true", check=False)

        run_cmd("echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true | debconf-set-selections", check=False)

        run_cmd("apt-get update -y")
        run_cmd("apt-get install -y --no-install-recommends ca-certificates curl wget gpg gpg-agent apt-transport-https lsb-release")

        Path("/etc/apt/keyrings").mkdir(parents=True, exist_ok=True)
        os.chmod("/etc/apt/keyrings", 0o755)

        # 1. Docker CE
        run_cmd("curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc")
        os.chmod("/etc/apt/keyrings/docker.asc", 0o644)
        distro_id = self.os_info.get("ID", "debian")
        codename = self.os_info.get("VERSION_CODENAME", "bookworm")
        if codename in ("sid", "testing", "unstable"):
            codename = "trixie"
        repo_url = f"https://download.docker.com/linux/{distro_id}"
        Path("/etc/apt/sources.list.d/docker.list").write_text(
            f"deb [arch=amd64,arm64 signed-by=/etc/apt/keyrings/docker.asc] {repo_url} {codename} stable\n"
        )

        # 2. Mise
        run_cmd("curl -fsSL https://mise.jdx.dev/gpg-key.pub | gpg --dearmor --yes -o /etc/apt/keyrings/mise-archive-keyring.gpg")
        os.chmod("/etc/apt/keyrings/mise-archive-keyring.gpg", 0o644)
        Path("/etc/apt/sources.list.d/mise.list").write_text(
            "deb [signed-by=/etc/apt/keyrings/mise-archive-keyring.gpg] https://mise.jdx.dev/deb stable main\n"
        )

        # 3. Eza
        run_cmd("curl -fsSL https://raw.githubusercontent.com/eza-community/eza/main/deb.asc | gpg --dearmor --yes -o /etc/apt/keyrings/gierens.gpg")
        os.chmod("/etc/apt/keyrings/gierens.gpg", 0o644)
        Path("/etc/apt/sources.list.d/gierens.list").write_text(
            "deb [signed-by=/etc/apt/keyrings/gierens.gpg] http://deb.gierens.de stable main\n"
        )

        # 4. VS Code
        if config["vscode"]:
            run_cmd("curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor --yes -o /etc/apt/keyrings/packages.microsoft.gpg")
            os.chmod("/etc/apt/keyrings/packages.microsoft.gpg", 0o644)
            Path("/etc/apt/sources.list.d/vscode.list").write_text(
                "deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main\n"
            )

        # 5. Antigravity IDE
        if config["antigravity"]:
            run_cmd("curl -fsSL https://us-central1-apt.pkg.dev/doc/repo-signing-key.gpg | gpg --dearmor --yes -o /etc/apt/keyrings/antigravity-repo-key.gpg")
            os.chmod("/etc/apt/keyrings/antigravity-repo-key.gpg", 0o644)
            Path("/etc/apt/sources.list.d/antigravity.list").write_text(
                "deb [signed-by=/etc/apt/keyrings/antigravity-repo-key.gpg] https://us-central1-apt.pkg.dev/projects/antigravity-auto-updater-dev/ antigravity-debian main\n"
            )

        run_cmd("apt-get update -y")

    def system_upgrade(self):
        log_info("Upgrading base system packages...")
        run_cmd("apt-get dist-upgrade -y")

    def install_system_packages(self, install_desktop_apps):
        log_info("Installing system compilers, core CLI tools, fonts, and runtimes...")
        core_pkgs = [
            "git", "curl", "wget", "tar", "unzip", "7zip", "jq", "make", "cmake", "clang", "ninja-build",
            "build-essential", "pkg-config", "xz-utils", "zstd", "binutils", "fontconfig", "sudo",
            "bat", "fd-find", "fzf", "ripgrep", "zoxide", "direnv", "micro", "btop", "inxi",
            "wl-clipboard", "xclip", "poppler-utils", "eza", "mise", "flatpak", "openssh-server",
            "fonts-firacode", "fonts-inter", "papirus-icon-theme", "ttf-mscorefonts-installer",
            "zsh", "podman"
        ]
        run_cmd(["apt-get", "install", "-y"] + core_pkgs, check=False)

        run_cmd("apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || apt-get install -y docker.io docker-compose || true", check=False)
        run_cmd("apt-get install -y ffmpeg libavcodec-extra mesa-va-drivers mesa-vdpau-drivers qemu-system qemu-utils libvirt-daemon-system virt-manager || true", check=False)

        if Path("/usr/bin/batcat").exists() and not Path("/usr/local/bin/bat").exists():
            os.symlink("/usr/bin/batcat", "/usr/local/bin/bat")
        if Path("/usr/bin/fdfind").exists() and not Path("/usr/local/bin/fd").exists():
            os.symlink("/usr/bin/fdfind", "/usr/local/bin/fd")

        if not shutil.which("starship"):
            log_info("Installing Starship prompt via standalone installer...")
            run_cmd("curl -sS https://starship.rs/install.sh | sh -s -- -y", check=False)

        if install_desktop_apps:
            log_info("Installing Desktop GUI Applications (Kitty, Foliate, qBittorrent, MPV)...")
            run_cmd("apt-get install -y kitty mpv foliate qbittorrent || true", check=False)

    def install_vscode(self):
        log_info("Installing Visual Studio Code via APT...")
        run_cmd("apt-get install -y code", check=False)

    def install_antigravity(self):
        log_info("Installing Google Antigravity IDE via APT...")
        run_cmd("apt-get install -y antigravity", check=False)

    def post_system_setup(self):
        log_info("Configuring system services and user groups...")
        for s in ["ssh", "docker", "containerd", "libvirtd"]:
            run_cmd(f"systemctl enable --now {s} 2>/dev/null || true", check=False)
        for g in ["sudo", "docker", "libvirt", "kvm"]:
            run_cmd(f"getent group {g} >/dev/null 2>&1 || groupadd {g}", check=False)
            run_cmd(f"usermod -aG {g} {self.target_user} 2>/dev/null || true", check=False)

# ==============================================================================
# Fedora / RHEL Distro Adapter
# ==============================================================================
class FedoraDistro(BaseDistro):
    def clean_legacy(self):
        log_info("Cleaning legacy Fedora repositories and Docker packages...")
        for pattern in ["/etc/yum.repos.d/mise.repo", "/etc/yum.repos.d/vscode.repo",
                        "/etc/yum.repos.d/docker-ce.repo", "/etc/yum.repos.d/terra*.repo"]:
            run_cmd(f"rm -f {pattern}", check=False)
        run_cmd("dnf remove -y docker docker-client docker-common containerd runc 2>/dev/null || true", check=False)

    def _resolve_working_terra_ver(self, raw_ver):
        """Finds the latest reachable Terra repository branch, falling back if unreleased."""
        candidates = [raw_ver, "42", "41", "40"]
        for ver in candidates:
            url = f"https://repos.fyralabs.com/terra{ver}/repodata/repomd.xml"
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status == 200:
                        return ver
            except Exception:
                continue
        return "41"

    def configure_package_manager(self, config):
        log_info("Configuring DNF settings and RPM repositories...")
        run_cmd("dnf install -y dnf-plugins-core dnf5-plugins 2>/dev/null || true", check=False)

        dnf_conf = Path("/etc/dnf/dnf.conf")
        dnf_conf.write_text(
            "[main]\ngpgcheck=True\ninstallonly_limit=3\nclean_requirements_on_remove=True\nskip_if_unavailable=True\nmax_parallel_downloads=10\n"
        )

        fedora_ver = run_cmd(["rpm", "-E", "%fedora"], capture=True).stdout.strip() or "41"

        # 1. Official Mise Repo (Avoids Terra single point of failure)
        log_info("Enabling official Mise repository...")
        Path("/etc/yum.repos.d/mise.repo").write_text(
            "[mise]\nname=mise\nbaseurl=https://mise.jdx.dev/rpm\nenabled=1\ngpgcheck=1\ngpgkey=https://mise.jdx.dev/gpg-key.pub\nskip_if_unavailable=True\n"
        )

        # 2. Official Starship COPR (Guarantees Starship availability on Fedora 41-44+)
        log_info("Enabling Starship COPR repository...")
        run_cmd("dnf copr enable -y atim/starship 2>/dev/null || true", check=False)

        # 3. Terra Repository with Verified Branch & Escaped Substitution
        if run_cmd("rpm -q terra-release", check=False).returncode != 0:
            log_info("Enabling Terra repository...")
            terra_ver = self._resolve_working_terra_ver(fedora_ver)
            log_info(f"Targeting Terra repository branch: {terra_ver}")
            res = run_cmd(
                f'dnf install -y --nogpgcheck --repofrompath "terra-bootstrap,https://repos.fyralabs.com/terra{terra_ver}" terra-release',
                check=False
            )
            if res.returncode != 0:
                log_warn("terra-release RPM bootstrap failed; configuring direct /etc/yum.repos.d/terra.repo fallback.")
                Path("/etc/yum.repos.d/terra.repo").write_text(
                    f"[terra]\nname=Terra {terra_ver}\nbaseurl=https://repos.fyralabs.com/terra{terra_ver}\nenabled=1\ngpgcheck=0\nskip_if_unavailable=True\n"
                )

        # 4. RPM Fusion (with rawhide/release fallback)
        log_info("Enabling RPM Fusion repositories...")
        fusion_cmd = (
            f'dnf install -y "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-{fedora_ver}.noarch.rpm" '
            f'"https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-{fedora_ver}.noarch.rpm" 2>/dev/null || '
            f'dnf install -y "https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-rawhide.noarch.rpm" '
            f'"https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-rawhide.noarch.rpm" || true'
        )
        run_cmd(fusion_cmd, check=False)

        # 5. Docker CE
        log_info("Enabling Docker CE repository...")
        run_cmd("dnf config-manager addrepo --from-repofile https://download.docker.com/linux/fedora/docker-ce.repo 2>/dev/null || curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo -o /etc/yum.repos.d/docker-ce.repo", check=False)

        # 6. VS Code (if selected)
        if config["vscode"]:
            run_cmd("rpm --import https://packages.microsoft.com/keys/microsoft.asc 2>/dev/null || true", check=False)
            Path("/etc/yum.repos.d/vscode.repo").write_text(
                "[code]\nname=Visual Studio Code\nbaseurl=https://packages.microsoft.com/yumrepos/vscode\nenabled=1\nautorefresh=1\ntype=rpm-md\ngpgcheck=1\ngpgkey=https://packages.microsoft.com/keys/microsoft.asc\nskip_if_unavailable=True\n"
            )

    def system_upgrade(self):
        log_info("Refreshing and upgrading DNF packages...")
        run_cmd("dnf upgrade --refresh -y --skip-unavailable")

    def install_system_packages(self, install_desktop_apps):
        log_info("Installing packages via DNF...")
        pkgs = [
            "git", "curl", "wget", "tar", "unzip", "p7zip", "p7zip-plugins", "jq", "xz", "zstd", "binutils",
            "fontconfig", "sudo", "make", "cmake", "clang", "ninja-build",
            "eza", "bat", "fzf", "ripgrep", "fd-find", "zoxide", "yazi", "direnv", "micro", "btop",
            "fastfetch", "inxi", "wl-clipboard", "xclip", "poppler-utils", "starship", "atuin", "mise",
            "flatpak", "openssh-server",
            "firacode-nerd-fonts", "rsms-inter-vf-fonts", "0xproto-nerd-fonts", "papirus-icon-theme",
            "zsh", "podman"
        ]
        run_cmd(["dnf", "install", "-y", "--skip-unavailable"] + pkgs, check=False)

        # Standalone Mise fallback
        if not shutil.which("mise"):
            log_info("Installing Mise via official standalone installer...")
            run_cmd("curl -fsSL https://mise.jdx.dev/install.sh | MISE_INSTALL_PATH=/usr/local/bin/mise sh", check=False)

        # Standalone Starship fallback
        if not shutil.which("starship"):
            log_info("Installing Starship prompt via official installer...")
            run_cmd("curl -sS https://starship.rs/install.sh | sh -s -- -y", check=False)

        # Docker CE / Moby
        run_cmd("dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || dnf install -y moby-engine docker-compose || true", check=False)

        # Multimedia & Codecs
        run_cmd("dnf swap -y ffmpeg-free ffmpeg --allowerasing --exclude=libheif-freeworld || dnf install -y ffmpeg || true", check=False)
        run_cmd("dnf swap -y mesa-va-drivers mesa-va-drivers-freeworld 2>/dev/null || true", check=False)
        run_cmd("dnf swap -y mesa-vdpau-drivers mesa-vdpau-drivers-freeworld 2>/dev/null || true", check=False)
        run_cmd("dnf install -y @virtualization 2>/dev/null || true", check=False)

        # Microsoft Fonts
        run_cmd("dnf install -y curl cabextract xorg-x11-font-utils fontconfig || true", check=False)
        run_cmd("rpm -i https://downloads.sourceforge.net/project/mscorefonts2/rpms/msttcore-fonts-installer-2.6-1.noarch.rpm 2>/dev/null || true", check=False)

        if install_desktop_apps:
            log_info("Installing Desktop GUI Applications (Kitty, Foliate, qBittorrent, MPV)...")
            run_cmd("dnf install -y --skip-unavailable kitty mpv foliate qbittorrent || true", check=False)

    def install_vscode(self):
        log_info("Installing Visual Studio Code via DNF...")
        run_cmd("dnf install -y code", check=False)

    def install_antigravity(self):
        log_info("Installing Google Antigravity IDE...")
        if run_cmd("dnf install -y antigravity 2>/dev/null", check=False).returncode == 0:
            log_success("Installed Antigravity natively from RPM repository.")
            return

        log_info("Resolving latest Antigravity package from Google Cloud Repository...")
        repo_base = "https://us-central1-apt.pkg.dev/projects/antigravity-auto-updater-dev"
        deb_url = f"{repo_base}/pool/main/a/antigravity/antigravity_1.23.2-1776332190_amd64.deb"

        try:
            packages_url = f"{repo_base}/dists/antigravity-debian/main/binary-amd64/Packages"
            req = urllib.request.Request(packages_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                pkg_text = resp.read().decode("utf-8", errors="ignore")
                match = re.search(r"^Filename:\s*(pool/main/.*\.deb)", pkg_text, re.MULTILINE)
                if match:
                    deb_url = f"{repo_base}/{match.group(1).strip()}"
        except Exception as e:
            log_warn(f"Could not scrape latest index, using pinned release URL: {e}")

        tmp_deb = "/tmp/antigravity.deb"
        try:
            log_info(f"Downloading Antigravity package from {deb_url}...")
            download_file(deb_url, tmp_deb)

            log_info("Extracting Antigravity package payload directly to root filesystem...")
            if not extract_deb_payload(tmp_deb, extract_root="/"):
                raise RuntimeError("Failed to locate or unpack data.tar archive inside .deb")

            # Binary path resolution
            bin_candidates = [
                "/usr/bin/antigravity",
                "/opt/antigravity/antigravity",
                "/opt/antigravity/bin/antigravity"
            ]
            actual_bin = next((b for b in bin_candidates if os.path.exists(b)), None)

            if actual_bin:
                os.chmod(actual_bin, 0o755)
                if actual_bin != "/usr/local/bin/antigravity" and not Path("/usr/local/bin/antigravity").exists():
                    os.symlink(actual_bin, "/usr/local/bin/antigravity")
                if actual_bin != "/usr/bin/antigravity" and not Path("/usr/bin/antigravity").exists():
                    os.symlink(actual_bin, "/usr/bin/antigravity")

            # Desktop launcher generation
            desktop_file = Path("/usr/share/applications/antigravity.desktop")
            if not desktop_file.exists() and actual_bin:
                icon_candidates = list(Path("/opt/antigravity").glob("**/*.png")) + list(Path("/opt/antigravity").glob("**/*.svg"))
                icon_path = str(icon_candidates[0]) if icon_candidates else "utilities-terminal"
                desktop_file.write_text(f"""[Desktop Entry]
Version=1.0
Type=Application
Name=Google Antigravity IDE
Exec="{actual_bin}" %F
Icon={icon_path}
Comment=Next-generation Cloud & AI IDE
Categories=Development;IDE;
Terminal=false
StartupWMClass=antigravity
""")
                os.chmod(desktop_file, 0o644)

            log_success("Google Antigravity IDE successfully deployed to /opt/antigravity")
        except Exception as e:
            log_error(f"Failed to deploy Google Antigravity IDE: {e}")
        finally:
            if os.path.exists(tmp_deb):
                os.remove(tmp_deb)

    def post_system_setup(self):
        log_info("Configuring system services and user groups...")
        for s in ["sshd", "docker", "containerd", "libvirtd"]:
            run_cmd(f"systemctl enable --now {s} 2>/dev/null || true", check=False)
        for g in ["wheel", "docker", "kvm"]:
            run_cmd(f"getent group {g} >/dev/null 2>&1 || groupadd {g}", check=False)
            run_cmd(f"usermod -aG {g} {self.target_user} 2>/dev/null || true", check=False)

# ==============================================================================
# Arch Linux Distro Adapter
# ==============================================================================
class ArchDistro(BaseDistro):
    def clean_legacy(self):
        log_info("Cleaning package cache and lock files...")
        run_cmd("rm -f /var/lib/pacman/db.lck", check=False)

    def configure_package_manager(self, config):
        log_info("Configuring Pacman and multilib repositories...")
        pac_conf = Path("/etc/pacman.conf")
        if pac_conf.exists():
            text = pac_conf.read_text()
            if "#ParallelDownloads" in text:
                text = text.replace("#ParallelDownloads = 5", "ParallelDownloads = 10")
            elif "ParallelDownloads" not in text:
                text = text.replace("[options]\n", "[options]\nParallelDownloads = 10\nColor\n")
            text = re.sub(r"#\[multilib\]\n#Include = /etc/pacman\.d/mirrorlist", "[multilib]\nInclude = /etc/pacman.d/mirrorlist", text)
            pac_conf.write_text(text)

        run_cmd("pacman -Sy --noconfirm")

    def system_upgrade(self):
        log_info("Upgrading Arch Linux system packages...")
        run_cmd("pacman -Syu --noconfirm")

    def _ensure_aur_helper(self):
        if shutil.which("yay"):
            return "yay"
        if shutil.which("paru"):
            return "paru"
        log_info("Bootstrapping yay AUR helper as user...")
        run_cmd("pacman -S --noconfirm --needed base-devel git", check=False)
        aur_tmp = f"/tmp/yay-bin-{self.target_user}"
        shutil.rmtree(aur_tmp, ignore_errors=True)
        run_cmd(f"git clone https://aur.archlinux.org/yay-bin.git {aur_tmp}", as_user=self.target_user)
        run_cmd(f"cd {aur_tmp} && makepkg -si --noconfirm", as_user=self.target_user)
        shutil.rmtree(aur_tmp, ignore_errors=True)
        return "yay" if shutil.which("yay") else None

    def install_system_packages(self, install_desktop_apps):
        log_info("Installing packages via Pacman...")
        pkgs = [
            "base-devel", "git", "curl", "wget", "tar", "unzip", "p7zip", "jq", "xz", "zstd", "binutils",
            "fontconfig", "sudo", "make", "cmake", "clang", "ninja", "pkgconf",
            "eza", "bat", "fzf", "ripgrep", "fd", "zoxide", "yazi", "direnv", "micro", "btop",
            "fastfetch", "inxi", "wl-clipboard", "xclip", "poppler", "starship", "atuin",
            "flatpak", "openssh",
            "ttf-fira-code", "ttf-inter", "papirus-icon-theme",
            "ffmpeg", "qemu-desktop", "virt-manager", "libvirt",
            "zsh", "podman", "docker", "docker-compose", "docker-buildx"
        ]
        run_cmd(["pacman", "-S", "--noconfirm", "--needed"] + pkgs, check=False)

        helper = self._ensure_aur_helper()
        if helper:
            log_info(f"Installing AUR tools (mise-bin) via {helper}...")
            run_cmd(f"{helper} -S --noconfirm --needed mise-bin ttf-ms-fonts || true", as_user=self.target_user, check=False)

        if install_desktop_apps:
            log_info("Installing Desktop GUI Applications (Kitty, Foliate, qBittorrent, MPV)...")
            run_cmd("pacman -S --noconfirm --needed kitty mpv foliate qbittorrent || true", check=False)

    def install_vscode(self):
        log_info("Installing Visual Studio Code via AUR...")
        helper = self._ensure_aur_helper()
        if helper:
            run_cmd(f"{helper} -S --noconfirm visual-studio-code-bin || true", as_user=self.target_user, check=False)
        else:
            run_cmd("pacman -S --noconfirm code || true", check=False)

    def install_antigravity(self):
        log_info("Installing Google Antigravity IDE via AUR...")
        helper = self._ensure_aur_helper()
        if helper:
            run_cmd(f"{helper} -S --noconfirm antigravity-ide || true", as_user=self.target_user, check=False)

    def post_system_setup(self):
        log_info("Configuring system services and user groups...")
        for s in ["sshd", "docker", "libvirtd"]:
            run_cmd(f"systemctl enable --now {s} 2>/dev/null || true", check=False)
        for g in ["wheel", "docker", "kvm", "libvirt"]:
            run_cmd(f"getent group {g} >/dev/null 2>&1 || groupadd {g}", check=False)
            run_cmd(f"usermod -aG {g} {self.target_user} 2>/dev/null || true", check=False)

# ==============================================================================
# Universal Core (Shared Components)
# ==============================================================================
class UniversalCore:
    def __init__(self, script_dir, target_user, target_home, target_uid, target_gid):
        self.script_dir = Path(script_dir)
        self.target_user = target_user
        self.target_home = Path(target_home)
        self.target_uid = target_uid
        self.target_gid = target_gid

    def _safe_deploy(self, src_file: Path, dst_file: Path):
        """Copies dotfile with automatic backup and strict ownership management."""
        if not src_file.exists():
            return

        dst_file.parent.mkdir(parents=True, exist_ok=True)
        os.chown(dst_file.parent, self.target_uid, self.target_gid)

        # Create timestamped backup if destination exists
        if dst_file.exists():
            backup_path = dst_file.with_suffix(f"{dst_file.suffix}.bak-{int(time.time())}")
            shutil.copy2(dst_file, backup_path)
            os.chown(backup_path, self.target_uid, self.target_gid)
            log_info(f"Existing file backed up: {backup_path}")

        shutil.copy2(src_file, dst_file)
        os.chown(dst_file, self.target_uid, self.target_gid)
        os.chmod(dst_file, 0o644)
        log_info(f"Deployed: {dst_file}")

    def deploy_dotfiles(self, install_desktop_apps):
        log_section("Deploying Fixed Dotfiles")
        dotfiles_dir = self.script_dir / "dotfiles"
        if not dotfiles_dir.exists():
            log_warn(f"Dotfiles directory {dotfiles_dir} not found. Skipping dotfiles.")
            return

        # 1. Shell configs
        self._safe_deploy(dotfiles_dir / "zsh" / ".zshrc", self.target_home / ".zshrc")
        self._safe_deploy(dotfiles_dir / "zsh" / ".zsh_aliases", self.target_home / ".zsh_aliases")

        # 2. Starship prompt
        self._safe_deploy(dotfiles_dir / "starship" / "starship.toml", self.target_home / ".config" / "starship.toml")

        # 3. Kitty Terminal
        if install_desktop_apps or shutil.which("kitty"):
            kitty_src = dotfiles_dir / "kitty"
            if kitty_src.exists():
                kitty_dst = self.target_home / ".config" / "kitty"
                kitty_dst.mkdir(parents=True, exist_ok=True)
                os.chown(kitty_dst, self.target_uid, self.target_gid)
                for conf in kitty_src.glob("*.conf"):
                    self._safe_deploy(conf, kitty_dst / conf.name)

    def bootstrap_zinit(self):
        log_section("Bootstrapping Zinit Plugin Manager")
        zinit_dir = self.target_home / ".local" / "share" / "zinit" / "zinit.git"
        if not zinit_dir.exists():
            log_info("Cloning zdharma-continuum/zinit.git...")
            run_cmd(f"mkdir -p '{zinit_dir.parent}' && git clone https://github.com/zdharma-continuum/zinit.git '{zinit_dir}'", as_user=self.target_user, check=False)
        else:
            log_info("Zinit is already present.")

    def install_nerd_fonts(self):
        log_section("Deploying System-Wide Nerd Fonts")
        font_dir = Path("/usr/local/share/fonts/NerdFonts")
        font_check = font_dir / "FiraCodeNerdFont-Regular.ttf"
        if font_check.exists():
            log_info("Nerd Fonts (FiraCode, 0xProto) are already installed.")
            return

        font_dir.mkdir(parents=True, exist_ok=True)
        log_info("Downloading FiraCode & 0xProto Nerd Fonts from official GitHub releases...")
        urls = [
            "https://github.com/ryanoasis/nerd-fonts/releases/latest/download/FiraCode.tar.xz",
            "https://github.com/ryanoasis/nerd-fonts/releases/latest/download/0xProto.tar.xz"
        ]
        for url in urls:
            tmp_tar = tempfile.mktemp(suffix=".tar.xz")
            try:
                download_file(url, tmp_tar)
                run_cmd(f"tar -xJf {tmp_tar} -C {font_dir} 2>/dev/null || true")
            except Exception as e:
                log_warn(f"Failed to download Nerd Font from {url}: {e}")
            finally:
                if os.path.exists(tmp_tar):
                    os.remove(tmp_tar)
        run_cmd("fc-cache -f 2>/dev/null || true", check=False)
        log_success("Nerd Fonts deployed to /usr/local/share/fonts/NerdFonts")

    def install_jetbrains_rider(self):
        log_section("Installing JetBrains Rider (.NET IDE)")
        if Path("/opt/rider/bin/rider.sh").exists():
            log_info("JetBrains Rider is already installed at /opt/rider.")
            return

        try:
            log_info("Querying JetBrains Release API...")
            api_url = "https://data.services.jetbrains.com/products/releases?code=RD&latest=true&type=release"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                dl_url = data["RD"][0]["downloads"]["linux"]["link"]

            log_info(f"Downloading JetBrains Rider from {dl_url}...")
            tmp_tar = tempfile.mktemp(suffix=".tar.gz")
            download_file(dl_url, tmp_tar)

            opt_rider = Path("/opt/rider")
            opt_rider.mkdir(parents=True, exist_ok=True)
            run_cmd(f"tar -xzf {tmp_tar} -C {opt_rider} --strip-components=1")
            os.remove(tmp_tar)

            if not Path("/usr/local/bin/rider").exists():
                os.symlink("/opt/rider/bin/rider.sh", "/usr/local/bin/rider")
            run_cmd("chmod -R 755 /opt/rider", check=False)

            icon = "/opt/rider/bin/rider.svg" if Path("/opt/rider/bin/rider.svg").exists() else "/opt/rider/bin/rider.png"
            desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name=JetBrains Rider
Icon={icon}
Exec="/usr/local/bin/rider" %f
Comment=Cross-platform .NET IDE
Categories=Development;IDE;
Terminal=false
StartupWMClass=jetbrains-rider
"""
            Path("/usr/share/applications/jetbrains-rider.desktop").write_text(desktop_content)
            run_cmd("chmod 644 /usr/share/applications/jetbrains-rider.desktop", check=False)
            log_success("JetBrains Rider installed successfully at /opt/rider")
        except Exception as e:
            log_warn(f"Failed to install JetBrains Rider: {e}")

    def install_android_studio(self):
        log_section("Installing Android Studio")
        if Path("/opt/android-studio/bin/studio.sh").exists():
            log_info("Android Studio is already installed at /opt/android-studio.")
            return

        try:
            log_info("Scraping latest Android Studio Linux archive URL...")
            page_url = "https://developer.android.com/studio"
            req = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            match = re.search(r'https://[^"\'\s]+android-studio[^"\'\s]+linux\.tar\.gz', html)
            if not match:
                log_warn("Could not find Android Studio Linux download link.")
                return
            dl_url = match.group(0)

            log_info(f"Downloading Android Studio from {dl_url}...")
            tmp_tar = tempfile.mktemp(suffix=".tar.gz")
            download_file(dl_url, tmp_tar)

            opt_studio = Path("/opt/android-studio")
            opt_studio.mkdir(parents=True, exist_ok=True)
            run_cmd(f"tar -xzf {tmp_tar} -C {opt_studio} --strip-components=1")
            os.remove(tmp_tar)

            if not Path("/usr/local/bin/studio").exists():
                os.symlink("/opt/android-studio/bin/studio.sh", "/usr/local/bin/studio")
            run_cmd("chmod -R 755 /opt/android-studio", check=False)

            icon = "/opt/android-studio/bin/studio.svg" if Path("/opt/android-studio/bin/studio.svg").exists() else "/opt/android-studio/bin/studio.png"
            desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name=Android Studio
Icon={icon}
Exec="/usr/local/bin/studio" %f
Comment=Official Android IDE
Categories=Development;IDE;
Terminal=false
StartupWMClass=jetbrains-studio
"""
            Path("/usr/share/applications/android-studio.desktop").write_text(desktop_content)
            run_cmd("chmod 644 /usr/share/applications/android-studio.desktop", check=False)
            log_success("Android Studio installed successfully at /opt/android-studio")
        except Exception as e:
            log_warn(f"Failed to install Android Studio: {e}")

    def configure_mise(self):
        log_section("Configuring Mise Global Developer Runtimes")
        mise_bin = shutil.which("mise") or "/usr/local/bin/mise"
        if os.path.exists(mise_bin):
            log_info("Setting up dotnet@10, node@latest, and java@lts via Mise...")
            run_cmd(f"'{mise_bin}' settings set idiomatic_version_file false 2>/dev/null || true", as_user=self.target_user, check=False)
            run_cmd(f"'{mise_bin}' settings set yes true 2>/dev/null || true", as_user=self.target_user, check=False)
            run_cmd(f"'{mise_bin}' use --global dotnet@10 node@latest java@lts 2>/dev/null || true", as_user=self.target_user, check=False)
        else:
            log_warn("Mise runtime manager not found in PATH or /usr/local/bin/mise.")

    def setup_ssh_key(self):
        log_section("Configuring User SSH Keys")
        ssh_dir = self.target_home / ".ssh"
        key_file = ssh_dir / "id_ed25519"
        if not key_file.exists():
            ssh_dir.mkdir(parents=True, exist_ok=True)
            os.chown(ssh_dir, self.target_uid, self.target_gid)
            os.chmod(ssh_dir, 0o700)
            run_cmd(f"ssh-keygen -t ed25519 -f '{key_file}' -N ''", as_user=self.target_user, check=False)
            pub_file = ssh_dir / "id_ed25519.pub"
            if pub_file.exists():
                if shutil.which("wl-copy"):
                    run_cmd(f"wl-copy < '{pub_file}' 2>/dev/null || true", as_user=self.target_user, check=False)
                elif shutil.which("xclip"):
                    run_cmd(f"xclip -selection clipboard < '{pub_file}' 2>/dev/null || true", as_user=self.target_user, check=False)
                log_success("Generated SSH Ed25519 key and attempted copying to clipboard.")
        else:
            log_info("SSH Ed25519 key already exists.")

    def configure_shell(self):
        log_section("Setting Default Shell to Zsh")
        zsh_bin = shutil.which("zsh") or "/bin/zsh"
        if os.path.exists(zsh_bin):
            run_cmd(f"chsh -s {zsh_bin} {self.target_user} 2>/dev/null || usermod -s {zsh_bin} {self.target_user} 2>/dev/null || true", check=False)
            log_success(f"Default shell set to {zsh_bin} for user {self.target_user}")

# ==============================================================================
# CLI Argument Parser & User Intervention
# ==============================================================================
def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Universal Cross-Distro Post-Installation Setup (Debian/Ubuntu, Fedora/RHEL, Arch Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python3 install.py                 # Interactive prompts for IDEs & Desktop Apps
  sudo python3 install.py -y              # Install all components non-interactively
  sudo python3 install.py --default       # Safe defaults (skips optional IDEs & desktop apps)
  sudo python3 install.py --vscode --antigravity --no-desktop-apps
"""
    )
    parser.add_argument("-y", "--yes", "--all", action="store_true", help="Install all components including all IDEs and Desktop Apps (non-interactive)")
    parser.add_argument("--default", action="store_true", help="Non-interactive execution using safe defaults (skips optional IDEs and Desktop Apps)")
    parser.add_argument("--desktop-apps", dest="desktop_apps", action="store_const", const=True, help="Install Desktop GUI Applications (Kitty, Foliate, qBittorrent, MPV)")
    parser.add_argument("--no-desktop-apps", dest="desktop_apps", action="store_const", const=False, help="Skip Desktop GUI Applications")
    parser.add_argument("--ides", dest="ides", action="store_const", const=True, help="Install all IDEs (VS Code, Antigravity, Rider, Android Studio)")
    parser.add_argument("--no-ides", dest="ides", action="store_const", const=False, help="Skip all IDEs")
    parser.add_argument("--vscode", dest="vscode", action="store_const", const=True, help="Install Visual Studio Code")
    parser.add_argument("--no-vscode", dest="vscode", action="store_const", const=False, help="Skip Visual Studio Code")
    parser.add_argument("--antigravity", dest="antigravity", action="store_const", const=True, help="Install Google Antigravity IDE")
    parser.add_argument("--no-antigravity", dest="antigravity", action="store_const", const=False, help="Skip Google Antigravity IDE")
    parser.add_argument("--rider", dest="rider", action="store_const", const=True, help="Install JetBrains Rider")
    parser.add_argument("--no-rider", dest="rider", action="store_const", const=False, help="Skip JetBrains Rider")
    parser.add_argument("--studio", dest="studio", action="store_const", const=True, help="Install Android Studio")
    parser.add_argument("--no-studio", dest="studio", action="store_const", const=False, help="Skip Android Studio")
    return parser.parse_args()

def resolve_configuration(args):
    """Interactively prompts user for options if not passed via CLI flags."""
    non_interactive = args.yes or args.default or not sys.stdin.isatty()

    config = {
        "desktop_apps": args.desktop_apps,
        "vscode": args.vscode,
        "antigravity": args.antigravity,
        "rider": args.rider,
        "studio": args.studio
    }

    if args.yes:
        for k in config:
            config[k] = True
        return config, non_interactive

    if args.ides is not None:
        for ide_key in ["vscode", "antigravity", "rider", "studio"]:
            if config[ide_key] is None:
                config[ide_key] = args.ides

    if config["desktop_apps"] is None:
        print()
        config["desktop_apps"] = prompt_yn(
            "Install Desktop GUI Applications (Kitty, Foliate, qBittorrent, MPV)?",
            default_no=True,
            non_interactive=non_interactive
        )

    ide_keys_unset = [k for k in ["vscode", "antigravity", "rider", "studio"] if config[k] is None]
    if ide_keys_unset:
        print()
        if prompt_yn("Configure and install Development IDEs?", default_no=True, non_interactive=non_interactive):
            if prompt_yn("  Install ALL available IDEs (VS Code, Antigravity, Rider, Android Studio)?", default_no=True, non_interactive=non_interactive):
                for k in ide_keys_unset:
                    config[k] = True
            else:
                if config["vscode"] is None:
                    config["vscode"] = prompt_yn("    Install Visual Studio Code?", default_no=True, non_interactive=non_interactive)
                if config["antigravity"] is None:
                    config["antigravity"] = prompt_yn("    Install Google Antigravity IDE?", default_no=True, non_interactive=non_interactive)
                if config["rider"] is None:
                    config["rider"] = prompt_yn("    Install JetBrains Rider (.NET IDE)?", default_no=True, non_interactive=non_interactive)
                if config["studio"] is None:
                    config["studio"] = prompt_yn("    Install Android Studio?", default_no=True, non_interactive=non_interactive)
        else:
            for k in ide_keys_unset:
                config[k] = False

    for k in config:
        if config[k] is None:
            config[k] = False

    return config, non_interactive

# ==============================================================================
# Main Orchestrator
# ==============================================================================
def main():
    args = parse_arguments()

    if os.geteuid() != 0:
        log_error("This script must be run with root privileges (sudo).")
        log_info("Usage: sudo python3 install.py [OPTIONS]")
        sys.exit(1)

    target_user = get_target_user()
    try:
        user_info = pwd.getpwnam(target_user)
        target_home = user_info.pw_dir
        target_uid = user_info.pw_uid
        target_gid = user_info.pw_gid
    except KeyError:
        log_error(f"Failed to resolve user account: {target_user}")
        sys.exit(1)

    distro_family, os_info = detect_distro_family()
    distro_pretty = os_info.get("PRETTY_NAME", os_info.get("NAME", "Linux"))

    log_section("Cross-Distro Post-Installation Bootstrap")
    print(f" Target User:   {target_user} (UID: {target_uid}, Home: {target_home})")
    print(f" Distribution:  {distro_pretty} [Family: {distro_family.upper()}]")
    print(f" Architecture:  {run_cmd(['uname', '-m'], capture=True).stdout.strip()}")

    if distro_family == "unknown":
        log_error(f"Unsupported distribution family: {os_info.get('ID')}. Supported: Debian/Ubuntu, Fedora/RHEL, Arch.")
        sys.exit(1)

    config, non_interactive = resolve_configuration(args)

    def fmt_bool(val):
        return f"{Colors.GREEN}YES{Colors.RESET}" if val else f"{Colors.YELLOW}NO{Colors.RESET}"

    log_section("Installation Configuration Summary")
    print(f" Desktop GUI Apps (Kitty, Foliate, etc): {fmt_bool(config['desktop_apps'])}")
    print(f" Visual Studio Code:                     {fmt_bool(config['vscode'])}")
    print(f" Google Antigravity IDE:                 {fmt_bool(config['antigravity'])}")
    print(f" JetBrains Rider (.NET):                 {fmt_bool(config['rider'])}")
    print(f" Android Studio:                         {fmt_bool(config['studio'])}")
    print("=" * 68)

    adapter = None
    if distro_family == "debian":
        adapter = DebianDistro(target_user, target_home, os_info)
    elif distro_family == "fedora":
        adapter = FedoraDistro(target_user, target_home, os_info)
    elif distro_family == "arch":
        adapter = ArchDistro(target_user, target_home, os_info)

    script_dir = Path(__file__).resolve().parent
    core = UniversalCore(script_dir, target_user, target_home, target_uid, target_gid)

    # Execution Pipeline
    log_section("1. Cleaning Legacy Repositories & Packages")
    adapter.clean_legacy()

    log_section("2. Configuring Package Manager & Repositories")
    adapter.configure_package_manager(config)

    log_section("3. Upgrading System Packages")
    adapter.system_upgrade()

    log_section("4. Installing Compilers, CLI Tools & Desktop Apps")
    adapter.install_system_packages(config["desktop_apps"])

    log_section("5. Installing Selected Development IDEs")
    if config["vscode"]:
        adapter.install_vscode()
    else:
        log_info("Skipping Visual Studio Code.")

    if config["antigravity"]:
        adapter.install_antigravity()
    else:
        log_info("Skipping Google Antigravity IDE.")

    if config["rider"]:
        core.install_jetbrains_rider()
    else:
        log_info("Skipping JetBrains Rider.")

    if config["studio"]:
        core.install_android_studio()
    else:
        log_info("Skipping Android Studio.")

    # Universal Core Tasks
    core.install_nerd_fonts()
    core.bootstrap_zinit()
    core.deploy_dotfiles(config["desktop_apps"])
    core.configure_mise()
    core.setup_ssh_key()
    core.configure_shell()

    # Distro Post-Setup
    log_section("6. Post-Installation Services & User Groups")
    adapter.post_system_setup()
    run_cmd("update-desktop-database /usr/share/applications 2>/dev/null || true", check=False)

    log_section("Post-Installation Setup Completed Successfully!")
    print("\nComponent Summary:")
    print(f" • Distribution Base:     {distro_pretty} [Updated & Configured]")
    print(f" • Terminal & Shell:      Zsh + Zinit + Fixed Dotfiles (.zshrc, .zsh_aliases)")
    print(f" • Fonts & Icons:         Nerd Fonts (FiraCode, 0xProto) System-Wide")
    print(f" • Developer Runtimes:    Mise (Dotnet 10, Node.js, Java LTS)")
    print(f" • Desktop Applications:  {'Installed' if config['desktop_apps'] else 'Skipped'}")
    print(f" • Visual Studio Code:    {'Installed' if config['vscode'] else 'Skipped'}")
    print(f" • Antigravity IDE:       {'Installed' if config['antigravity'] else 'Skipped'}")
    print(f" • JetBrains Rider:       {'Installed' if config['rider'] else 'Skipped'}")
    print(f" • Android Studio:        {'Installed' if config['studio'] else 'Skipped'}")
    print("=" * 68)

    print(f"\n{Colors.GREEN}{Colors.BOLD}To activate your new shell, groups, and dotfiles immediately without rebooting, run:{Colors.RESET}")
    print(f"  {Colors.CYAN}exec sg docker -c \"exec zsh -l\"{Colors.RESET}\n")

    if not non_interactive:
        if prompt_yn("Would you like to reboot now to finalize all system daemon and session changes?", default_no=True):
            log_info("Rebooting system...")
            run_cmd("reboot 2>/dev/null || systemctl reboot 2>/dev/null || echo 'Please reboot manually.'", check=False)
        else:
            log_info("Reboot skipped. Please execute the command above to refresh your active session.")
    else:
        log_info("Non-interactive run completed.")

if __name__ == "__main__":
    main()
