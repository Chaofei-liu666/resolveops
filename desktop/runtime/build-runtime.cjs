const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const runtimeRoot = __dirname;
const projectRoot = path.resolve(runtimeRoot, '..', '..');
const python = process.env.RESOLVEOPS_PYTHON || 'py';
const pythonPrefix = process.env.RESOLVEOPS_PYTHON ? [] : ['-3'];
const separator = process.platform === 'win32' ? ';' : ':';
const venvRoot = path.join(runtimeRoot, '.venv');
const venvPython = process.platform === 'win32'
  ? path.join(venvRoot, 'Scripts', 'python.exe')
  : path.join(venvRoot, 'bin', 'python');
const outputRoot = path.join(runtimeRoot, 'win');

function run(command, args) {
  const result = spawnSync(command, args, { cwd: projectRoot, stdio: 'inherit' });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status || 1);
}

if (!fs.existsSync(venvPython)) {
  run(python, [...pythonPrefix, '-m', 'venv', venvRoot]);
}
run(venvPython, ['-m', 'pip', 'install', '-r', path.join(runtimeRoot, 'requirements-build.txt')]);

for (const name of ['.build', 'win', 'resolveops-runtime']) {
  fs.rmSync(path.join(runtimeRoot, name), { recursive: true, force: true });
}

run(venvPython, [
  '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', 'resolveops-runtime',
  '--distpath', outputRoot,
  '--workpath', path.join(runtimeRoot, '.build'),
  '--specpath', path.join(runtimeRoot, '.build'),
  '--paths', projectRoot,
  '--collect-submodules', 'production',
  '--add-data', `${path.join(projectRoot, 'production', 'migrations')}${separator}production/migrations`,
  '--add-data', `${path.join(projectRoot, 'static')}${separator}static`,
  path.join(runtimeRoot, 'entry.py'),
]);

console.log(`Portable runtime created: ${path.join(outputRoot, 'resolveops-runtime', 'resolveops-runtime.exe')}`);
