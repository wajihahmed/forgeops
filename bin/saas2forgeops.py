#!/usr/bin/env python3
"""
Apply saas patch documents onto forgeops base config files, or mirror AM tree
config from a live pod for alpha/bravo realms.

Subcommands:

  managed   -- patches managed.json (adds/removes IDM managed object types)
  repo-ds   -- patches repo.ds.json (adds resource mappings for saas object types)
  access    -- patches access.json  (adds saas roles and access policy entries)
  am-mirror -- mirrors root realm tree/node config from a live AM pod into
               am-conf/ for alpha and bravo realms (no saas repo needed)

All IDM subcommands are idempotent: running them against an already-merged file
produces the same output as running them against the original base.

am-mirror subcommand:
  Requires a running AM pod. Uses kubectl cp to pull the root realm's config from
  the pod, then generates alpha/bravo versions by substituting realm names and uid
  suffixes. Produces both instance files (uuid.json) and service singleton
  (default.json) files. Must be re-run whenever the Login tree structure changes.
  Does NOT require or use the saas repo.

  After running, push to Gitea:
    python3 bin/mock-tenant.py push-config --target am

Usage:
    # Extract bases from running IDM pod
    IDM_POD=$(kubectl get pod -n fr-platform -l app=idm -o jsonpath='{.items[0].metadata.name}')
    kubectl exec -n fr-platform $IDM_POD -- cat /opt/openidm/conf/managed.json > /tmp/base-managed.json
    kubectl exec -n fr-platform $IDM_POD -- cat /opt/openidm/conf/repo.ds.json  > /tmp/base-repo.ds.json
    kubectl exec -n fr-platform $IDM_POD -- cat /opt/openidm/conf/access.json   > /tmp/base-access.json

    SAAS=/path/to/saas/services/idm/idm-idc-overrides/system

    python3 bin/saas2forgeops.py managed \\
        /tmp/base-managed.json $SAAS/managed.json > /tmp/merged-managed.json

    python3 bin/saas2forgeops.py repo-ds \\
        /tmp/base-repo.ds.json $SAAS/repo.ds.json > /tmp/merged-repo.ds.json

    python3 bin/saas2forgeops.py access \\
        /tmp/base-access.json $SAAS/access.json > /tmp/merged-access.json

    python3 bin/saas2forgeops.py am-mirror [--namespace fr-platform] [--am-conf kustomize/base/gitea-seed/am-conf]

Notes:
  managed subcommand:
    - "remove /objects[/name eq \"foo\"]" ops are skipped if the object is absent.
    - "add /objects/-" ops are skipped if an object with the same name already exists.
    - svcacct's scopes policy references "&{fraas.svcacct.allowed.scopes}" — set this ESV
      variable via the ESV shim before IDM starts or the policy evaluates against an empty list.

  repo-ds subcommand:
    - /ldapConnectionFactories is always skipped — it contains saas-specific hostnames
      (userstore-0.userstore, userstore-2.userstore) that would break IDM's connection to
      ds-idrepo. The forgeops base already has the correct ds-idrepo hostname.
    - "replace" and "add" operations on any other path navigate the JSON tree (URL-decoded
      path segments), creating intermediate dicts as needed. Idempotent by nature.

  access subcommand:
    - Handles IDM's array-filter patch syntax on the top-level "configs" array:
        remove  /configs[/field op "value" and ...]  -- remove matching entries
        replace /configs[/field op "value" and ...]  -- replace first matching entry
        append  configs/-                            -- append all "elements" to configs,
                                                        skipping exact duplicates
    - Supported filter operators: eq (equals), co (contains).

  am-mirror subcommand:
    - Only whitelisted service dirs are mirrored (see _AM_MIRROR_SAFE_DIRS).
    - Service config dirs from AIC (oauth2provider, scriptingservice, etc.) are excluded —
      they are in AIC export format and cause NPE in ConfigEntityConverter on forgeops AM.
    - sunidentityrepositoryservice (opendj.json) is always included — it configures the
      identity store pointing at ou=user,o={realm},o=root,ou=identities.
    - Both instance files (uuid.json) and service singletons (default.json) are generated.
      Singletons are required: without them the DS subtree does not exist and the FBC
      importer cannot write instance entries, causing NodeProcessException at auth time.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote


# ── managed.json helpers ──────────────────────────────────────────────────────

def _managed_extract_name(field):
    m = re.search(r'/objects\[/name eq "([^"]+)"\]', field)
    if not m:
        raise ValueError(f"Cannot parse filter field: {field!r}")
    return m.group(1)


def merge_managed(base, patches):
    objects = base.get("objects", [])

    for op in patches:
        operation = op["operation"]
        field = op["field"]

        if operation == "remove":
            name = _managed_extract_name(field)
            before = len(objects)
            objects = [o for o in objects if o.get("name") != name]
            if len(objects) == before:
                print(f"  skip remove: '{name}' not in base (ok)", file=sys.stderr)
            else:
                print(f"  removed: '{name}'", file=sys.stderr)

        elif operation == "add" and field == "/objects/-":
            value = op["value"]
            name = value.get("name", "<unnamed>")
            if any(o.get("name") == name for o in objects):
                print(f"  skip add: '{name}' already present (idempotent)", file=sys.stderr)
            else:
                objects.append(value)
                print(f"  added: '{name}'", file=sys.stderr)

        else:
            print(f"  WARNING: unsupported op '{operation}' on '{field}' — skipped",
                  file=sys.stderr)

    base["objects"] = objects
    return base


# ── repo.ds.json helpers ──────────────────────────────────────────────────────

_REPO_DS_SKIP_PREFIXES = {"/ldapConnectionFactories"}


def _parse_path(field):
    """'/a/b%2Fc/d' -> ['a', 'b/c', 'd']"""
    parts = field.lstrip("/").split("/")
    return [unquote(p) for p in parts]


def _set_path(doc, parts, value):
    node = doc
    for key in parts[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    node[parts[-1]] = value


def merge_repo_ds(base, patches):
    for op in patches:
        operation = op["operation"]
        field = op["field"]

        top = "/" + field.lstrip("/").split("/")[0]
        if top in _REPO_DS_SKIP_PREFIXES:
            print(f"  skip (connection config): {field}", file=sys.stderr)
            continue

        if operation in ("replace", "add"):
            parts = _parse_path(field)
            _set_path(base, parts, op["value"])
            print(f"  {operation}: {field}", file=sys.stderr)
        else:
            print(f"  WARNING: unsupported op '{operation}' on '{field}' — skipped",
                  file=sys.stderr)

    return base


# ── access.json helpers ───────────────────────────────────────────────────────

def _parse_array_filter(field):
    """
    '/configs[/pattern co "fidc" and /roles eq "*"]'
        -> array_key='configs', filter_str='/pattern co ...'
    """
    m = re.match(r'/?(\w+)\[(.+)\]$', field.strip())
    if m:
        return m.group(1), m.group(2)
    key = field.lstrip("/").split("[")[0].rstrip("/-")
    return key, None


def _matches_filter(item, filter_str):
    """Evaluate an IDM array-filter expression (eq, co, and) against a dict."""
    conditions = re.split(r'\s+and\s+', filter_str, flags=re.IGNORECASE)
    for cond in conditions:
        cond = cond.strip()
        m = re.match(r'/(\S+)\s+(eq|co)\s+"([^"]*)"', cond)
        if not m:
            print(f"  WARNING: unparseable filter condition: {cond!r}", file=sys.stderr)
            return False
        field, operator, value = m.group(1), m.group(2), m.group(3)
        item_val = str(item.get(field, ""))
        if operator == "eq" and item_val != value:
            return False
        if operator == "co" and value not in item_val:
            return False
    return True


def merge_access(base, patches):
    configs = base.get("configs", [])

    for op in patches:
        operation = op["operation"]
        field = op.get("field", "")

        if operation == "remove":
            array_key, filter_str = _parse_array_filter(field)
            if array_key != "configs" or not filter_str:
                print(f"  WARNING: unsupported remove target '{field}' — skipped",
                      file=sys.stderr)
                continue
            before = len(configs)
            configs = [c for c in configs if not _matches_filter(c, filter_str)]
            removed = before - len(configs)
            print(f"  remove {removed} entry(s) matching: {filter_str}", file=sys.stderr)

        elif operation == "replace":
            array_key, filter_str = _parse_array_filter(field)
            if array_key != "configs" or not filter_str:
                print(f"  WARNING: unsupported replace target '{field}' — skipped",
                      file=sys.stderr)
                continue
            replaced = False
            for i, item in enumerate(configs):
                if _matches_filter(item, filter_str):
                    configs[i] = op["value"]
                    replaced = True
                    break
            if replaced:
                print(f"  replaced entry matching: {filter_str}", file=sys.stderr)
            else:
                print(f"  skip replace: no entry matched: {filter_str}", file=sys.stderr)

        elif operation == "append":
            elements = op.get("elements", [])
            added = 0
            for elem in elements:
                if elem in configs:
                    print(f"  skip append: entry already present (idempotent): "
                          f"{json.dumps(elem, separators=(',', ':'))[:80]}",
                          file=sys.stderr)
                else:
                    configs.append(elem)
                    added += 1
            if added:
                print(f"  appended {added} new entries ({len(elements)-added} already present)",
                      file=sys.stderr)

        else:
            print(f"  WARNING: unsupported op '{operation}' on '{field}' — skipped",
                  file=sys.stderr)

    base["configs"] = configs
    return base


# ── am-mirror helpers ─────────────────────────────────────────────────────────

_AM_MIRROR_SAFE_DIRS = {
    # Authentication tree node types
    "authenticationtreesservice",
    "pagenode",
    "validatedusernamenode",
    "validatedpasswordnode",
    "identitystoredecisionnode",
    "datastoredecisionnode",
    "incrementlogincountnode",
    "innertreeevaluatornode",
    "retrylimitdecisionnode",
    "accountlockoutnode",
    "logincountdecisionnode",
    "queryfilterdecisionnode",
    "patchobjectnode",
    "attributecollectornode",
    # Realm services (visible in AM admin console Services page)
    # Confirmed present in root realm on forgeops AM and required in alpha/bravo
    "sunidentityrepositoryservice",   # External Data Stores — identity store config
    "oauth2provider",                  # OAuth2 Provider — required for idm-provisioning client
    "idmintegrationservice",           # IDM Integration — IDM endpoint for PatchObjectNode/IncrementLoginCountNode
    "amrealmbaseurl",                  # Base URL Source
    "selfservicetrees",                # Self Service Trees
    "socialidentityproviders",         # Social Identity Provider Service
    "validationservice",               # Validation Service
}

_AM_MIRROR_REALMS = ("alpha", "bravo")
_AM_ROOT_UID_SUFFIX = "ou=am-config"


def _am_mirror_get_pod(namespace):
    r = subprocess.run(
        ["kubectl", "get", "pod", "-n", namespace, "-l", "app=am",
         "-o", "jsonpath={.items[0].metadata.name}"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _am_mirror_copy_root(am_pod, namespace, tmpdir):
    root_path = "/home/forgerock/openam/config/services/realm/root"
    dst = os.path.join(tmpdir, "root")
    subprocess.run(
        ["kubectl", "cp", "-n", namespace, f"{am_pod}:{root_path}", dst],
        check=True,
    )
    return dst


def _am_mirror_patch_idrepo(data, realm):
    """
    Patch the identity store (opendj.json) data section so it points at the
    correct LDAP subtree for the given realm.

    Root realm uses:
      organization_name = ou=identities
      people-container-value = people
      psearchbase = ou=identities

    Sub-realms (alpha, bravo) use:
      organization_name = o={realm},o=root,ou=identities
      people-container-value = user
      psearchbase = o={realm},o=root,ou=identities

    Confirmed against AIC production alpha realm snapshot.
    """
    base_dn = f"o={realm},o=root,ou=identities"
    for section in data.values():
        if not isinstance(section, dict):
            continue
        if "sun-idrepo-ldapv3-config-organization_name" in section:
            section["sun-idrepo-ldapv3-config-organization_name"] = base_dn
        if "sun-idrepo-ldapv3-config-psearchbase" in section:
            section["sun-idrepo-ldapv3-config-psearchbase"] = base_dn
        if "sun-idrepo-ldapv3-config-people-container-value" in section:
            section["sun-idrepo-ldapv3-config-people-container-value"] = "user"


def _am_mirror_realm(root_dir, realm, am_conf_dir):
    dst_realm = os.path.join(am_conf_dir, "realm", f"root-{realm}")
    realm_suffix = f"o={realm},ou=services,ou=am-config"
    written = 0

    for dirpath, _, filenames in os.walk(root_dir):
        rel_dir = os.path.relpath(dirpath, root_dir)
        parts = rel_dir.split(os.sep)
        top = parts[0]
        if top not in _AM_MIRROR_SAFE_DIRS:
            continue

        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            # Skip service singleton default.json — generated separately
            if fname == "default.json" and parts[-1] == "organizationconfig":
                continue

            src = os.path.join(dirpath, fname)
            dst = os.path.join(dst_realm, rel_dir, fname)
            os.makedirs(os.path.dirname(dst), exist_ok=True)

            with open(src) as f:
                doc = json.load(f)

            # Patch realm and uid suffix in metadata
            raw = json.dumps(doc)
            raw = raw.replace('"realm" : "/"', f'"realm" : "/{realm}"')
            raw = raw.replace('"realm": "/"', f'"realm": "/{realm}"')
            raw = raw.replace(_AM_ROOT_UID_SUFFIX, realm_suffix)
            doc = json.loads(raw)

            # Patch identity store base DN for sub-realm identity lookup
            if (top == "sunidentityrepositoryservice"
                    and fname == "opendj.json"
                    and isinstance(doc.get("data"), dict)):
                _am_mirror_patch_idrepo(doc["data"], realm)

            # Inject identityResource on tree definitions that contain IDM nodes.
            # Required so IncrementLoginCountNode/LoginCountDecisionNode/PatchObjectNode
            # know which managed object to operate on. Root realm doesn't set this
            # (uses managed/user), so am-mirror must add it for sub-realms.
            _IDM_NODES = {
                "IncrementLoginCountNode", "LoginCountDecisionNode",
                "PatchObjectNode", "QueryFilterDecisionNode",
            }
            if (top == "authenticationtreesservice"
                    and doc.get("data", {}).get("nodes") is not None
                    and "identityResource" not in doc["data"]):
                nodes = doc["data"]["nodes"]
                if any(n.get("nodeType") in _IDM_NODES for n in nodes.values()):
                    doc["data"]["identityResource"] = f"managed/{realm}_user"

            # Rewrite identityResource in node instance files (e.g. PatchObjectNode).
            # Root realm nodes carry managed/user; sub-realm nodes must use
            # managed/{realm}_user or AM raises:
            #   "Configured identity resource for the node does not match the tree"
            if (top in ("patchobjectnode", "queryfilterdecisionnode",
                        "incrementlogincountnode", "logincountdecisionnode")
                    and isinstance(doc.get("data"), dict)
                    and doc["data"].get("identityResource") == "managed/user"):
                doc["data"]["identityResource"] = f"managed/{realm}_user"

            with open(dst, "w") as f:
                json.dump(doc, f, indent=2)
            written += 1

    return written


def _am_mirror_service_singletons(root_dir, realm, am_conf_dir):
    """
    Generate organizationconfig/default.json for each whitelisted service dir
    by copying from root realm and adapting realm/uid.
    """
    realm_suffix = f"o={realm},ou=services,ou=am-config"
    written = 0

    for svc in _AM_MIRROR_SAFE_DIRS:
        src = os.path.join(root_dir, svc, "1.0", "organizationconfig", "default.json")
        if not os.path.exists(src):
            continue

        with open(src) as f:
            content = f.read()

        content = content.replace('"realm" : "/"', f'"realm" : "/{realm}"')
        content = content.replace('"realm": "/"', f'"realm": "/{realm}"')
        content = content.replace(_AM_ROOT_UID_SUFFIX, realm_suffix)

        dst_dir = os.path.join(am_conf_dir, "realm", f"root-{realm}",
                               svc, "1.0", "organizationconfig")
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "default.json")

        with open(dst, "w") as f:
            f.write(content)
        written += 1

    return written


def cmd_am_mirror(argv):
    os.chdir(os.path.join(os.path.dirname(__file__), ".."))

    parser = argparse.ArgumentParser(
        prog="saas2forgeops.py am-mirror",
        description="Mirror root realm tree/node config from a live AM pod into am-conf/.",
    )
    parser.add_argument("--namespace", default="fr-platform")
    parser.add_argument("--am-conf", default="kustomize/base/gitea-seed/am-conf")
    args = parser.parse_args(argv)

    print(f"Getting AM pod in namespace {args.namespace}...")
    am_pod = _am_mirror_get_pod(args.namespace)
    print(f"AM pod: {am_pod}")

    with tempfile.TemporaryDirectory() as tmpdir:
        print("Copying root realm config from AM pod...")
        root_dir = _am_mirror_copy_root(am_pod, args.namespace, tmpdir)

        for realm in _AM_MIRROR_REALMS:
            print(f"\nMirroring root → {realm}...")
            n = _am_mirror_realm(root_dir, realm, args.am_conf)
            s = _am_mirror_service_singletons(root_dir, realm, args.am_conf)
            print(f"  {n} instance files + {s} service singletons written")

    print("\nDone. Push to Gitea with:")
    print("  python3 bin/mock-tenant.py push-config --target am")


# ── main ──────────────────────────────────────────────────────────────────────

SUBCOMMANDS = ("managed", "repo-ds", "access", "am-mirror")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in SUBCOMMANDS:
        print(
            "Usage:\n"
            + "\n".join(f"  saas2forgeops.py {s}  <base> <patch>"
                        for s in ("managed", "repo-ds", "access"))
            + "\n  saas2forgeops.py am-mirror  [--namespace fr-platform] [--am-conf <dir>]"
            + "\n\nIDM output is written to stdout. am-mirror writes files in-place.",
            file=sys.stderr,
        )
        sys.exit(1)

    subcommand = sys.argv[1]

    if subcommand == "am-mirror":
        cmd_am_mirror(sys.argv[2:])
        return

    if len(sys.argv) != 4:
        print(
            f"Usage: saas2forgeops.py {subcommand}  <base> <patch>",
            file=sys.stderr,
        )
        sys.exit(1)

    base_path, patch_path = sys.argv[2], sys.argv[3]

    with open(base_path) as f:
        base = json.load(f)
    with open(patch_path) as f:
        patches = json.load(f)

    print(f"Subcommand: {subcommand}", file=sys.stderr)
    print(f"Patch operations: {len(patches)}", file=sys.stderr)

    if subcommand == "managed":
        print(f"Base objects: {len(base.get('objects', []))}", file=sys.stderr)
        result = merge_managed(base, patches)
        print(f"Result objects: {len(result.get('objects', []))}", file=sys.stderr)
    elif subcommand == "repo-ds":
        result = merge_repo_ds(base, patches)
    else:
        print(f"Base configs: {len(base.get('configs', []))}", file=sys.stderr)
        result = merge_access(base, patches)
        print(f"Result configs: {len(result.get('configs', []))}", file=sys.stderr)

    print(json.dumps(result, indent=4))


if __name__ == "__main__":
    main()
