# ADR 0005: Stable storage identity

Status: accepted

Persist filesystem UUID where available and compare mounted-device identity before enabling deletion. Never bind policies to `/dev/sdX`.

Reason: Linux block-device names are unstable.

Consequence: layered/device-mapper storage may need richer physical-device topology validation in a later hardware milestone.
