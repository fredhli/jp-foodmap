# Station-layer performance gate

`station_layer_bench.py` is the release gate for the default-on station layer.
It compares `stations=0` and `stations=1` on the exact same generated `docs/`
build and always uses the formal 8,954-station payload. It covers Fold inner,
Fold outer, and desktop viewports; 4x/6x CPU throttling; a gzip response shaped
to mobile latency/downlink; cold and warm cache; ten-drag CPU/frame/long-task
samples; forced-GC heap; DOM growth; and a 60-second idle CPU/draw/network
energy proxy.

Run the release gate:

```console
.venv-wsl/bin/python tests/perf/station_layer_bench.py
```

The default output is `tests/perf/results/station-layer-4.3.0/`. A shortened
development run can be requested with `--idle-seconds` and `--repeats`, but it
is labelled `DIAGNOSTIC` and can never authorize a release.
