# Nous ComfyUI maintenance

Install `nous_maintenance` as a ComfyUI custom node package by copying the
directory to the sidecar's `custom_nodes/` directory. The package registers its
route on the next ComfyUI start. Configure the Nous backend to reach ComfyUI over
loopback so the maintenance request is accepted.

The route queues an execution cache reset with model unloading disabled.
Nous reports request acceptance and prevents maintenance while ComfyUI has
running or queued jobs.
