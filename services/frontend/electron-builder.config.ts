import type { Configuration } from 'electron-builder';

const config: Configuration = {
  appId: 'ai.labmate.desktop',
  productName: 'Labmate',
  files: ['dist/**/*', 'dist-electron/**/*', 'package.json'],
  directories: { output: 'release' },
  mac: { target: 'dmg', category: 'public.app-category.developer-tools' },
  win: { target: 'nsis' },
  linux: { target: 'AppImage', category: 'Development' },
};

export default config;
