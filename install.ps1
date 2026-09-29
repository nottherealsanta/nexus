# Nexus installer for Windows (plans/install.md, phase 4).
#
# The Nexus daemon needs Unix domain sockets and POSIX process control, so native
# Windows is not supported yet. Use WSL and run the one-line installer there:
#
#   curl -LsSf https://raw.githubusercontent.com/nottherealsanta/nexus/main/install.sh | sh
$ErrorActionPreference = "Stop"
Write-Host "nexus-install: native Windows is not supported yet."
Write-Host "nexus-install: install WSL (wsl --install), then run this inside it:"
Write-Host "  curl -LsSf https://raw.githubusercontent.com/nottherealsanta/nexus/main/install.sh | sh"
exit 2
