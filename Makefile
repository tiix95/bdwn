# ------------------------------------------------------------------------------
# bdwn - Debian package build
# ------------------------------------------------------------------------------
VERSION    := 0.5.0
GIT_COMMIT := $(shell git rev-parse --short HEAD 2>/dev/null || echo "nogit")
IMAGE_NAME := bdwn
DEB_VER    := $(VERSION)+git$(GIT_COMMIT)
DEB_ARCH   := all
DEB_NAME   := $(IMAGE_NAME)_$(DEB_VER)_$(DEB_ARCH)

REPO       := git.un1verse.space
REPO_USER  := tiix
REPO_CA    := ${UNIVERSE_GITROOT}/pooki/pki/ca.crt


.DEFAULT_GOAL := help
.PHONY: help deb push-deb clean

## help      : Show this help message
help:
	@grep -E '^## ' Makefile | sed 's/^## /  /'

## deb       : Build the Debian package (output: ./$(DEB_NAME).deb)
deb:
	printf '$(IMAGE_NAME) ($(DEB_VER)) trixie; urgency=medium\n\n  * Build from git commit $(GIT_COMMIT)\n\n -- Tom <tom@localhost>  $(shell date -R)\n' > debian/changelog
	chmod +x debian/rules
	dpkg-buildpackage -us -uc -b
	mv ../$(DEB_NAME).deb ../$(IMAGE_NAME)_$(DEB_VER)_*.buildinfo ../$(IMAGE_NAME)_$(DEB_VER)_*.changes .

## push-deb  : Push the Debian package to the gitea repo
push-deb: deb
	curl -n --cacert $(REPO_CA) \
		--upload-file $(DEB_NAME).deb \
		https://$(REPO)/api/packages/$(REPO_USER)/debian/pool/trixie/main/upload

## clean     : Remove all build artifacts
clean:
	-rm -f bdwn*.deb bdwn*.buildinfo bdwn*.changes
	-dh_clean 2>/dev/null || true
	-rm -f $(DEB_NAME).deb $(IMAGE_NAME)_$(DEB_VER)_*.buildinfo $(IMAGE_NAME)_$(DEB_VER)_*.changes
