$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

if (Get-Command py -ErrorAction SilentlyContinue) {
    $basePython = "py"
    $baseArguments = @("-3.11")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $basePython = "python"
    $baseArguments = @()
} else {
    throw "Python 3.11 was not found"
}

& $basePython @baseArguments -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "CPython 3.11 is required"
}

$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
if ($architecture -eq "x64") {
    $architecture = "x86_64"
}

$buildVenv = Join-Path $projectRoot ".runtime\native-build-venv-windows-$architecture"
$venvPython = Join-Path $buildVenv "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    & $basePython @baseArguments -m venv $buildVenv
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the build environment"
    }
}

& $venvPython -m pip install --disable-pip-version-check "Cython==3.1.1"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Cython"
}

$outputDir = Join-Path $projectRoot ".runtime\native-license\windows-$architecture-cpython311"
$buildDir = Join-Path $projectRoot ".runtime\native-license-build\windows-$architecture-cpython311"

& $venvPython "$projectRoot\scripts\build_license_native.py" --output-dir $outputDir --build-dir $buildDir
if ($LASTEXITCODE -ne 0) {
    throw "Native module build failed"
}
