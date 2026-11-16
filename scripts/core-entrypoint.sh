#!/bin/sh
# Deliberately no override and no native launcher in the preparation revision.
# A playback lease, arbitrary receipt, profile or explicit service selection
# cannot establish the missing core writer admission and native containment.
printf '%s\n' 'core startup denied: writer lease and restored containment pending' >&2
exit 78
