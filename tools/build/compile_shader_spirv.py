#!/usr/bin/env python3

"""Compiles a single shader to SPIR-V and generates a C header.

Usage: compile_shader_spirv.py <input_path> <output_path>

Pipeline:
  1. glslangValidator -> unoptimized .spv
  2. spirv-opt -> optimized .spv
  3. spirv-dis -> disassembly .txt
  4. Generate .h with disassembly comment + uint32_t array
"""

import os
import shutil
import struct
import subprocess
import sys


SPIRV_STAGES = {
    "vs": "vert", "hs": "tesc", "ds": "tese",
    "gs": "geom", "ps": "frag", "cs": "comp",
}

XESL_WRAPPER = (
    "#version 460\n"
    "#extension GL_EXT_control_flow_attributes : require\n"
    "#extension GL_EXT_samplerless_texture_functions : require\n"
    "#extension GL_GOOGLE_include_directive : require\n"
    "#include \"%s\"\n"
)


# ROCKNIX/Odin: the cross-compiled ARM64 builder image's Ubuntu glslang-tools/
# spirv-tools packages don't always land in VULKAN_SDK, and its spirv-opt
# rejected this script's original --canonicalize-ids flag. find_vulkan_tools
# now falls back to PATH per-tool via shutil.which (so a partial toolchain
# still resolves what it has) and main() degrades gracefully when spirv-opt/
# spirv-dis are missing instead of hard failing the whole shader build.
def find_vulkan_tools():
    """Find Vulkan SDK tools via VULKAN_SDK env or PATH."""
    def find_tool(name):
        candidates = [name]
        if os.name == "nt" and not name.endswith(".exe"):
            candidates.insert(0, name + ".exe")
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return None

    vulkan_sdk = os.environ.get("VULKAN_SDK")
    if vulkan_sdk:
        bin_dir = os.path.join(vulkan_sdk, "bin")
        if os.path.isdir(bin_dir):
            tools = []
            for name in ("glslangValidator", "spirv-opt", "spirv-dis"):
                exe_name = name + ".exe" if os.name == "nt" else name
                path = os.path.join(bin_dir, exe_name)
                tools.append(path if os.path.isfile(path) else None)
            if tools[0]:
                return tuple(tools)

    # Fall back to PATH
    return (
        find_tool("glslangValidator"),
        find_tool("spirv-opt"),
        find_tool("spirv-dis"),
    )


def parse_stage(filename):
    """Extract the 2-char shader stage from filename like 'foo.cs.xesl'."""
    basename = os.path.splitext(filename)[0]  # 'foo.cs'
    identifier = basename.replace(".", "_")    # 'foo_cs'
    stage_key = identifier[-2:]
    if stage_key not in SPIRV_STAGES:
        return None, None
    return stage_key, SPIRV_STAGES[stage_key]


def write_stderr(data):
    # ROCKNIX/Odin: subprocess.run was sometimes returning stderr as bytes
    # depending on the failing tool, which crashed sys.stderr.write directly.
    if not data:
        return
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    sys.stderr.write(data)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input_path> <output_path>", file=sys.stderr)
        return 1

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    src_name = os.path.basename(input_path)
    src_dir = os.path.dirname(input_path)
    src_is_xesl = src_name.endswith(".xesl")

    stage_key, spirv_stage = parse_stage(src_name)
    if spirv_stage is None:
        print(f"ERROR: cannot determine shader stage from: {src_name}", file=sys.stderr)
        return 1

    # Compute identifier (matches what Lua does: basename with dots -> underscores)
    identifier = os.path.splitext(src_name)[0].replace(".", "_")

    glslang, spirv_opt, spirv_dis = find_vulkan_tools()
    if not glslang:
        print("ERROR: glslangValidator not found via VULKAN_SDK or PATH",
              file=sys.stderr)
        return 1

    # Create output directory if needed.
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Temp file paths next to output.
    base = os.path.splitext(output_path)[0]
    glslang_spv = base + ".glslang.spv"
    opt_spv = base + ".spv"
    dis_txt = base + ".txt"

    try:
        # Step 1: glslangValidator
        glslang_args = [
            glslang,
            "--stdin" if src_is_xesl else input_path,
            "-DSHADING_LANGUAGE_GLSL_XE=1",
            "-S", spirv_stage,
            "-o", glslang_spv,
            "-V",
        ]
        if src_is_xesl:
            glslang_args.append(f"-I{src_dir}")

        stdin_data = (XESL_WRAPPER % src_name) if src_is_xesl else None
        result = subprocess.run(glslang_args, input=stdin_data, text=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode != 0:
            print(f"ERROR: glslangValidator failed for {src_name}", file=sys.stderr)
            write_stderr(result.stderr)
            return 1

        # Step 2: spirv-opt, if available. The ARM64 builder image's
        # spirv-tools build rejected --canonicalize-ids outright, so it has
        # been dropped; falling back to unoptimized SPIR-V is also safe if
        # spirv-opt itself isn't present at all (e.g. a minimal toolchain).
        if spirv_opt:
            result = subprocess.run([
                spirv_opt, "-O", "-O",
                glslang_spv, "-o", opt_spv,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if result.returncode != 0:
                print(f"WARNING: spirv-opt failed for {src_name}; "
                      "using unoptimized SPIR-V", file=sys.stderr)
                write_stderr(result.stderr)
                shutil.copyfile(glslang_spv, opt_spv)
        else:
            shutil.copyfile(glslang_spv, opt_spv)

        # Step 3: spirv-dis, if available. Only used for the disassembly
        # comment in the generated header, so its absence isn't fatal.
        if spirv_dis:
            result = subprocess.run([spirv_dis, "-o", dis_txt, opt_spv],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if result.returncode != 0:
                print(f"ERROR: spirv-dis failed for {src_name}", file=sys.stderr)
                write_stderr(result.stderr)
                return 1

        # Step 4: Generate header
        with open(output_path, "w") as out:
            out.write("// Generated with `xb buildshaders`.\n#if 0\n")
            if os.path.exists(dis_txt):
                with open(dis_txt, "r") as dis_file:
                    dis_data = dis_file.read()
                    if dis_data:
                        out.write(dis_data)
                        if dis_data[-1] != "\n":
                            out.write("\n")
            out.write("#endif\n\nconst uint32_t %s[] = {" % identifier)
            with open(opt_spv, "rb") as spv_file:
                index = 0
                while True:
                    word = spv_file.read(4)
                    if len(word) == 0:
                        break
                    if len(word) != 4:
                        print("ERROR: SPIR-V binary is misaligned", file=sys.stderr)
                        return 1
                    if index % 6 == 0:
                        out.write("\n    ")
                    else:
                        out.write(" ")
                    index += 1
                    value = struct.unpack("<I", word)[0]
                    out.write("0x%08X," % value)
            out.write("\n};\n")

    finally:
        # Clean up intermediate files.
        for f in (glslang_spv, opt_spv, dis_txt):
            if os.path.exists(f):
                os.remove(f)

    return 0


if __name__ == "__main__":
    sys.exit(main())
