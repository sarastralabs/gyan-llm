#!/bin/sh
# Starts LibreChat after giving the built page its Gyan title and colors.
#
# LibreChat's index.html is built into the image with "LibreChat" as its title (the app changes
# it after loading, but the tab flashes the old name). Rewriting a few strings here avoids
# maintaining a fork of the source; the edit is applied again on every container start.
set -eu

PAGE=/app/client/dist/index.html
if [ -f "$PAGE" ]; then
  sed -i \
    -e 's|<title>LibreChat</title>|<title>Gyan</title>|' \
    -e 's|LibreChat - An open source chat application with support for multiple AI models|Gyan by Sarastra Labs: AI answers with sources|' \
    -e 's|<meta name="theme-color" content="#171717" />|<meta name="theme-color" content="#FF5C00" />|' \
    "$PAGE"
fi

exec npm run backend
