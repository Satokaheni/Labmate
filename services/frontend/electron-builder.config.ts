import type { Configuration } from 'electron-builder';

const config: Configuration = {
  appId: 'ai.labmate.desktop',
  productName: 'Labmate',
  files: ['dist/**/*', 'dist-electron/**/*', 'package.json'],
  directories: { output: 'release', buildResources: 'build' },
  icon: 'build/icon.png',
  mac: {
    target: [{ target: 'dmg', arch: ['arm64', 'x64'] }],
    category: 'public.app-category.developer-tools',
    icon: 'build/icon.png',
  },
  dmg: {
    title: 'Labmate',
    window: { width: 540, height: 380 },
    contents: [
      { x: 140, y: 180, type: 'file' },
      { x: 400, y: 180, type: 'link', path: '/Applications' },
    ],
  },
  win: { target: 'nsis', icon: 'build/icon.png' },
  linux: { target: 'AppImage', icon: 'build/icon.png', category: 'Development' },
};

export default config;
