# Realistic Water: hardware-compatibility patch

A patch for **Realistic Water** by **Skaffy**: https://github.com/sskaffy/Realistic-Water

This folder holds only the changes, not the mod. Apply it to the original source (commit `ccc10e7`, "1.3.1 UPDATE").
The original mod's license covers any build you make with it. If you share a build, it must follow that license's
fork conditions: credit Skaffy, link the original project, say it is a modified version, and keep the same license.

## What it changes

| Problem on weaker or unusual GPUs | Fix |
|---|---|
| `mg_solve_small`, `mg_coarse` and `mg_bottom` hardcode 1024-thread workgroups. Vulkan only guarantees 128, so the pressure solver fails on GPUs with lower limits. | New `GpuCaps` reads `maxComputeWorkGroupInvocations`, workgroup size and shared memory. Shaders get `RW_LARGE_GROUP`, the largest power of two (128 to 1024) the GPU supports. The coarsest multigrid level is sized to fit one group. |
| 7 particle shaders need subgroup arithmetic and ballot, which some drivers don't offer in compute shaders. | If they're missing, the subgroup calls become one-lane versions. The mod only uses subgroups to batch atomics, so results stay correct; it just does more atomics. |
| Every shader is recompiled on every launch, with a new shaderc compiler created for each one. | One compiler is reused, and compiled SPIR-V is cached in `.minecraft/.cache/realistic-water/spirv`, keyed by a SHA-256 of the final source. |
| The default settings are tuned for strong discrete GPUs. | New `hardwareProfile` setting (`auto`/`full`/`low`). On `auto`, integrated GPUs and GPUs with under 3 GB of VRAM get lighter defaults: resolution 6→4, lava 6→4, sand 12→8, substeps 4→2, surface subdivision 2→1, and fewer whitewater particles, sand grains and leaves, plus a smaller rain/kelp radius and less shadow detail. It only touches settings you never changed. |

The GPU it detects and the profile it picks are written to the game log.

## Apply and build

```sh
git clone https://github.com/sskaffy/Realistic-Water.git
cd Realistic-Water
git checkout ccc10e7
git apply /path/to/hardware-compat.patch
./gradlew build          # needs JDK 25; jar ends up in build/libs/
```

## What was checked

- `./gradlew build` passes with the patch applied (Minecraft 26.3, Fabric Loader 0.19.5).
- All 95 shaders compile with the mod's own shaderc in three setups: 1024 threads with subgroups, 256 threads with subgroups, and 128 threads with the subgroup fallback. The fallback SPIR-V uses no `GroupNonUniform` capabilities.
- **Not tested in-game.** There was no GPU to run it on, so frame-rate gains and how the lighter profile looks have not been measured.
