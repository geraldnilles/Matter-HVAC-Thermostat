# Supports standard packaging variables: DESTDIR, PREFIX, SYSCONFDIR, UNITDIR

# Installation directories
PREFIX ?= /usr
SYSCONFDIR ?= /etc
UNITDIR ?= /etc/systemd/system
DESTDIR ?=

# Source and destination paths
SRC_DIR = src
CONFIG_DIR = config
SYSTEMD_DIR = systemd

INSTALL_DIR = $(DESTDIR)$(PREFIX)/share/thermostat
INSTALL_TEMPLATE_DIR = $(INSTALL_DIR)/templates
INSTALL_CONFIG_DIR = $(DESTDIR)$(SYSCONFDIR)/thermostat
INSTALL_SYSTEMD_DIR = $(DESTDIR)$(UNITDIR)

# Python source files
PYTHON_FILES = $(SRC_DIR)/utils.py \
               $(SRC_DIR)/setup.py \
               $(SRC_DIR)/sensor.py \
               $(SRC_DIR)/control.py \
               $(SRC_DIR)/gpio.py \
               $(SRC_DIR)/mqtt.py \
               $(SRC_DIR)/web.py

# Template files
TEMPLATE_FILES = $(SRC_DIR)/templates/index.html

# Config files
CONFIG_FILES = $(CONFIG_DIR)/defaults.json

# Systemd service files
SYSTEMD_FILES = $(SYSTEMD_DIR)/thermostat-setup.service \
                $(SYSTEMD_DIR)/thermostat-sensor.service \
                $(SYSTEMD_DIR)/thermostat-control.service \
                $(SYSTEMD_DIR)/thermostat-gpio.service \
                $(SYSTEMD_DIR)/thermostat-mqtt.service \
                $(SYSTEMD_DIR)/thermostat-web.service

.PHONY: all install clean

all:
	@echo "Run 'make install' to install the thermostat system"

install: install-dirs install-python install-templates install-config install-systemd
	@echo "Installation complete"

install-dirs:
	install -d $(INSTALL_DIR)
	install -d $(INSTALL_TEMPLATE_DIR)
	install -d $(INSTALL_CONFIG_DIR)
	install -d $(INSTALL_SYSTEMD_DIR)

# Each install-* rule creates its own destination directory so the
# install target is safe under parallel make (-j). GNU make does not
# guarantee prerequisite ordering, so relying on install-dirs having
# run first is a race condition.
install-python: $(PYTHON_FILES)
	install -d $(INSTALL_DIR)
	install -m 644 $(SRC_DIR)/utils.py $(INSTALL_DIR)/
	install -m 755 $(SRC_DIR)/setup.py $(INSTALL_DIR)/
	install -m 755 $(SRC_DIR)/sensor.py $(INSTALL_DIR)/
	install -m 755 $(SRC_DIR)/control.py $(INSTALL_DIR)/
	install -m 755 $(SRC_DIR)/gpio.py $(INSTALL_DIR)/
	install -m 755 $(SRC_DIR)/mqtt.py $(INSTALL_DIR)/
	install -m 755 $(SRC_DIR)/web.py $(INSTALL_DIR)/

install-templates: $(TEMPLATE_FILES)
	install -d $(INSTALL_TEMPLATE_DIR)
	install -m 644 $(SRC_DIR)/templates/index.html $(INSTALL_TEMPLATE_DIR)/

install-config: $(CONFIG_FILES)
	install -d $(INSTALL_CONFIG_DIR)
	install -m 644 $(CONFIG_DIR)/defaults.json $(INSTALL_CONFIG_DIR)/

install-systemd: $(SYSTEMD_FILES)
	install -d $(INSTALL_SYSTEMD_DIR)
	install -m 644 $(SYSTEMD_DIR)/thermostat-setup.service $(INSTALL_SYSTEMD_DIR)/
	install -m 644 $(SYSTEMD_DIR)/thermostat-sensor.service $(INSTALL_SYSTEMD_DIR)/
	install -m 644 $(SYSTEMD_DIR)/thermostat-control.service $(INSTALL_SYSTEMD_DIR)/
	install -m 644 $(SYSTEMD_DIR)/thermostat-gpio.service $(INSTALL_SYSTEMD_DIR)/
	install -m 644 $(SYSTEMD_DIR)/thermostat-mqtt.service $(INSTALL_SYSTEMD_DIR)/
	install -m 644 $(SYSTEMD_DIR)/thermostat-web.service $(INSTALL_SYSTEMD_DIR)/

clean:
	@echo "Nothing to clean"
