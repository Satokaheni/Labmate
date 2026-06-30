import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * Channel parity test: ensure every ipcMain.handle in main.ts
 * has a corresponding ipcRenderer.invoke in preload.ts.
 * This catches missing bridge methods like getSkillDescriptors.
 */
describe('preload-bridge channel parity', () => {
  it('should have matching ipcMain.handle and ipcRenderer.invoke channels', () => {
    const mainPath = path.join(__dirname, 'main.ts');
    const preloadPath = path.join(__dirname, 'preload.ts');

    const mainContent = fs.readFileSync(mainPath, 'utf-8');
    const preloadContent = fs.readFileSync(preloadPath, 'utf-8');

    // Extract all ipcMain.handle channel names from main.ts
    // Pattern: ipcMain.handle('channel-name', ...)
    const handleMatches = mainContent.matchAll(/ipcMain\.handle\(['"]([^'"]+)['"]/g);
    const handleChannels = Array.from(handleMatches).map((m) => m[1]);

    // Extract all ipcRenderer.invoke channel names from preload.ts
    // Pattern: ipcRenderer.invoke('channel-name')
    const invokeMatches = preloadContent.matchAll(/ipcRenderer\.invoke\(['"]([^'"]+)['"]/g);
    const invokeChannels = Array.from(invokeMatches).map((m) => m[1]);

    // Also extract ipcRenderer.sendSync (e.g., for get-config, get-token)
    const sendSyncMatches = preloadContent.matchAll(/ipcRenderer\.sendSync\(['"]([^'"]+)['"]/g);
    const sendSyncChannels = Array.from(sendSyncMatches).map((m) => m[1]);

    const allPreloadChannels = new Set([...invokeChannels, ...sendSyncChannels]);

    // Check that all async handlers (ipcMain.handle) have invoke bridges
    for (const channel of handleChannels) {
      expect(allPreloadChannels).toContain(
        channel,
        `ipcMain.handle('${channel}', ...) in main.ts but no ipcRenderer.invoke/sendSync in preload.ts`
      );
    }
  });

  it('should expose getSkillDescriptors on electronAPI', () => {
    const preloadPath = path.join(__dirname, 'preload.ts');
    const preloadContent = fs.readFileSync(preloadPath, 'utf-8');

    // Check that getSkillDescriptors is explicitly named in the exposed API
    expect(preloadContent).toContain('getSkillDescriptors');
    expect(preloadContent).toContain('labmate:skill-descriptors');
    // Verify it's defined as a method that invokes the IPC channel
    expect(preloadContent).toMatch(/getSkillDescriptors.*ipcRenderer\.invoke.*labmate:skill-descriptors/s);
  });

  it('should have matching getMcpTools and getSkillDescriptors signatures', () => {
    const preloadPath = path.join(__dirname, 'preload.ts');
    const preloadContent = fs.readFileSync(preloadPath, 'utf-8');

    // Both should follow the same pattern: getMcpTools: () => ipcRenderer.invoke(...)
    // and getSkillDescriptors: () => ipcRenderer.invoke(...)
    expect(preloadContent).toMatch(/getMcpTools\s*:\s*\(\).*ipcRenderer\.invoke.*labmate:mcp-tools/s);
    expect(preloadContent).toMatch(
      /getSkillDescriptors\s*:\s*\(\).*ipcRenderer\.invoke.*labmate:skill-descriptors/s
    );
  });
});
