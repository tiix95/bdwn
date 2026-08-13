"""bdwn-webui - read-only status page for a bdwn backup server.

Reports the state and the size of every restic repository held in the chroot
jail built by bdwn-setup-backup-user. The repositories are encrypted and this
server holds no passphrase, so nothing here ever opens one: every figure comes
from the filesystem (sizes, file counts, mtimes).
"""

__version__ = "0.14.1"
