#!/usr/bin/env bash

# Author: wajih.ahmed@pingidentity.com
# Gets an AM OAuth2 access token via the client_credentials grant using the
# idm-provisioning client, against the mock-tenant dev stack (fr-platform).
# You must have jq installed in the path.
# Modeled after perf-tools/get_tok_pkce.sh, but client_credentials only —
# no user login, no PKCE, and no env.sh to source; everything is defined below.

set -euo pipefail

#################### Edit these as needed #####################################
TENANT="mock.iam.example.com"       # AM FQDN for this dev stack
REALM="root"                         # AM realm
NAMESPACE="fr-platform"               # k8s namespace
CI="idm-provisioning"                # client_id
SCOPE="fr:idm:*"                     # requested scope

#################### Do not edit below this line ##############################
URL="https://${TENANT}/am"
CS=$(kubectl get secret amster-env-secrets -n "${NAMESPACE}" -o jsonpath='{.data.IDM_PROVISIONING_CLIENT_SECRET}' | base64 -d)

echo "=> Requesting OAuth2 access token via client_credentials for client '${CI}' ..." ; echo ""

JSON=$(curl --silent --insecure --request POST \
  --data "grant_type=client_credentials" \
  --data "scope=${SCOPE}" \
  -u "${CI}:${CS}" \
  "${URL}/oauth2/realms/${REALM}/access_token")

AT=$(echo "${JSON}" | jq -r .access_token)

if [ "${AT}" = "null" ] || [ -z "${AT}" ]; then
  echo "=> Failed to get access token. Response:" ; echo ""
  echo "${JSON}" | jq .
  exit 1
fi

echo "=> OAuth2 Access Token: ${AT}" ; echo ""
echo "=> Saving access_token to file ./at.txt ..." ; echo ""
echo "${AT}" > at.txt

echo "=> Token Info:"
curl --silent --insecure "${URL}/oauth2/realms/${REALM}/tokeninfo?access_token=${AT}"
echo ""

exit 0
