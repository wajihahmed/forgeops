#!/bin/sh
#
# Applies DS settings from the saas repo (services/userstore/configuration/dsconfig-input)
# that are backend-independent and can run at Docker build time. Kept separate from
# mock-tenant-config.sh and ds-setup.sh for the same reason those are separate: avoids
# merge conflicts with upstream changes, and keeps each concern in its own file.
#
# Omits: Argon2/Scrypt memory pool ESV variable refs (no ESV in local cluster),
# OpenTelemetry plugin (no otel-agent), security relaxation (already in mock-tenant-config.sh),
# cfgStore/amIdentityStore index/backend settings (those backends don't exist until
# setup-profile runs at pod startup — handled in runtime-scripts/ds-idrepo/setup instead).
set -eux

/opt/opendj/bin/dsconfig --offline --no-prompt --batch <<END_OF_COMMAND_INPUT
set-global-configuration-prop --set trust-transaction-ids:true

set-password-storage-scheme-prop --scheme-name "Argon2" --set enabled:true
set-password-storage-scheme-prop --scheme-name "Bcrypt" --set enabled:true
set-password-storage-scheme-prop --scheme-name "PBKDF2-HMAC-SHA256" --set enabled:true
set-password-storage-scheme-prop --scheme-name "PBKDF2-HMAC-SHA512" --set enabled:true
set-password-storage-scheme-prop --scheme-name "PBKDF2-HMAC-SHA512T256" --set enabled:true
set-password-storage-scheme-prop --scheme-name "PBKDF2" --set enabled:true
set-password-storage-scheme-prop --scheme-name "PKCS5S2" --set enabled:true
set-password-storage-scheme-prop --scheme-name "Salted SHA-1" --set enabled:true
set-password-storage-scheme-prop --scheme-name "Salted SHA-256" --set enabled:true
set-password-storage-scheme-prop --scheme-name "Salted SHA-384" --set enabled:true
set-password-storage-scheme-prop --scheme-name "Salted SHA-512" --set enabled:true
set-password-storage-scheme-prop --scheme-name "SCRAM-SHA-256" --set enabled:true
set-password-storage-scheme-prop --scheme-name "SCRAM-SHA-512" --set enabled:true
set-password-storage-scheme-prop --scheme-name "Scrypt" --set enabled:true

set-password-policy-prop --policy-name "Default Password Policy" \
    --set allow-pre-encoded-passwords:true \
    --set deprecated-password-storage-scheme:Argon2 \
    --set deprecated-password-storage-scheme:Bcrypt \
    --set deprecated-password-storage-scheme:PBKDF2-HMAC-SHA256 \
    --set deprecated-password-storage-scheme:PBKDF2-HMAC-SHA512 \
    --set deprecated-password-storage-scheme:PBKDF2-HMAC-SHA512T256 \
    --set deprecated-password-storage-scheme:PBKDF2 \
    --set deprecated-password-storage-scheme:PKCS5S2 \
    --set "deprecated-password-storage-scheme:Salted SHA-1" \
    --set "deprecated-password-storage-scheme:Salted SHA-256" \
    --set "deprecated-password-storage-scheme:Salted SHA-384" \
    --set "deprecated-password-storage-scheme:Salted SHA-512" \
    --set deprecated-password-storage-scheme:SCRAM-SHA-256 \
    --set deprecated-password-storage-scheme:SCRAM-SHA-512 \
    --set deprecated-password-storage-scheme:Scrypt

set-connection-handler-prop --handler-name LDAP --advanced --set max-request-size:15mb

set-schema-provider-prop --provider-name "Core Schema" --set strict-format-postal-addresses:false
END_OF_COMMAND_INPUT

cd $DS_DATA_DIR
tar cvfz /opt/opendj/data.tar.gz *
