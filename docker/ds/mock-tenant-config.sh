#!/bin/sh
#
# Post-setup for the mock-tenant local dev deployment. Runs at docker build time, after
# ds-setup.sh has created the base PingDS instance and its priming tarball (data.tar.gz,
# expanded at pod startup to prime the PVC).
#
# Relaxes DS security settings that are secure-by-default in current PingDS releases. This
# local mock-tenant stack has no real client security requirements. Kept in its own file/RUN
# step rather than appended to ds-setup.sh: upstream ForgeOps has already removed this exact
# settings batch from ds-setup.sh once (FORGEOPS-4828, "move DS to secure by default") — living
# in a separate file avoids merge conflicts the next time ds-setup.sh changes upstream.
set -eux

/opt/opendj/bin/dsconfig --offline --no-prompt --batch <<END_OF_COMMAND_INPUT
set-global-configuration-prop --set "unauthenticated-requests-policy:allow"
set-password-policy-prop --policy-name "Default Password Policy" --set "require-secure-authentication:false" --set "require-secure-password-changes:false" --reset "password-validator"
set-password-policy-prop --policy-name "Root Password Policy" --set "require-secure-authentication:false" --set "require-secure-password-changes:false" --reset "password-validator"
END_OF_COMMAND_INPUT

# ds-setup.sh uses `if [ -f ldif-ext/identities/*.ldif ]` which silently skips all ldif-ext
# files when the glob matches more than one file. Append mock-tenant-orgs.ldif manually here.
# mock-tenant-orgs.ldif supersedes orgs.ldif: it contains the full realm hierarchy PLUS the
# AIC-parity OUs. The leading printf ensures the LDIF entry separator is present regardless
# of whether the preceding file has a trailing newline.
IDENTITY_STORE_PROFILE=/opt/opendj/template/setup-profiles/AM/identity-store/7.0/base-entries.ldif
printf '\n' >> ${IDENTITY_STORE_PROFILE}
cat /opt/opendj/ldif-ext/identities/mock-tenant-orgs.ldif >> ${IDENTITY_STORE_PROFILE}

# ds-setup.sh already built data.tar.gz before these settings were applied. Re-create it so the
# relaxed settings are captured in what primes the PVC at pod startup.
cd $DS_DATA_DIR
tar cvfz /opt/opendj/data.tar.gz *
