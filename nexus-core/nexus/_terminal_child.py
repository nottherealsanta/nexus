"""Acquire a controlling terminal before replacing this process with the shell."""
import fcntl
import os
import sys
import termios


def main() -> None:
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    shell = sys.argv[1]
    if len(sys.argv) > 2:
        os.execl(shell, shell, "-c", sys.argv[2])
    os.execl(shell, shell, "-i")


if __name__ == "__main__":
    main()
