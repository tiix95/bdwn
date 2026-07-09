# Projet BDWN - BackDown

This project aims to build a debian package to backup servers to a SFTP repo.

Backup must be restic format, encrypted with a passphrase. I can choose which folders to backup.

Backup should be a systemd timer + service. Service will run as an unpriv user

I will use a specific account with ssh key to sftp into the remote server. ssh config will be placed into unpriv user's home

Keep it as simple as possible.

Target :
    - Install bdwn deb package
    - Create a config file with list of directories, encryption passphrase, and all necessary config
    - Create ssh config to remote server
    - Start the systemd timer
    - Go

# tech

python and/or shell
restic
debian packages only as dependancies
