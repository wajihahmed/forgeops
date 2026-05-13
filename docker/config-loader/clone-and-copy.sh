#!/bin/sh
set -e

: "${GIT_URL:?GIT_URL is required}"
: "${GIT_PATH:?GIT_PATH is required}"
: "${CONFIG_SRC_PATH:?CONFIG_SRC_PATH is required}"
: "${DESTINATION_PATH:?DESTINATION_PATH is required}"
: "${CONFIG_LOAD_STRATEGY:?CONFIG_LOAD_STRATEGY is required}"

BRANCH="${BRANCH:-master}"

# Embed token into URL if provided
CLONE_URL="$GIT_URL"
if [ -n "$GIT_TOKEN" ]; then
    # Insert token as userinfo: http://token@host/path
    CLONE_URL=$(echo "$GIT_URL" | sed 's|://|://'"$GIT_TOKEN"'@|')
fi

echo "Cloning $GIT_URL branch=$BRANCH into $GIT_PATH"
git clone --depth=1 --branch "$BRANCH" "$CLONE_URL" "$GIT_PATH"

SRC="$GIT_PATH/$CONFIG_SRC_PATH"

if [ ! -d "$SRC" ]; then
    echo "ERROR: source path $SRC does not exist in repo" >&2
    exit 1
fi

echo "Strategy=$CONFIG_LOAD_STRATEGY  src=$SRC  dst=$DESTINATION_PATH"

case "$CONFIG_LOAD_STRATEGY" in
    JSON_REPLACE)
        mkdir -p "$DESTINATION_PATH"
        cp -rv "$SRC"/. "$DESTINATION_PATH/"
        ;;
    JSON_MERGE)
        mkdir -p "$DESTINATION_PATH"
        # Deep-merge each JSON file from src onto any existing file at dst.
        # For files that only exist in src, copy them straight across.
        find "$SRC" -type f -name "*.json" | while read -r src_file; do
            rel="${src_file#$SRC/}"
            dst_file="$DESTINATION_PATH/$rel"
            mkdir -p "$(dirname "$dst_file")"
            if [ -f "$dst_file" ]; then
                merged=$(jq -s '.[0] * .[1]' "$dst_file" "$src_file")
                echo "$merged" > "$dst_file"
            else
                cp "$src_file" "$dst_file"
            fi
        done
        # Copy non-JSON files straight across
        find "$SRC" -type f ! -name "*.json" | while read -r src_file; do
            rel="${src_file#$SRC/}"
            dst_file="$DESTINATION_PATH/$rel"
            mkdir -p "$(dirname "$dst_file")"
            cp "$src_file" "$dst_file"
        done
        ;;
    *)
        echo "ERROR: unknown CONFIG_LOAD_STRATEGY '$CONFIG_LOAD_STRATEGY'" >&2
        exit 1
        ;;
esac

echo "config-loader done"
