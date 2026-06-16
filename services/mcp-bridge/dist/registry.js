import { registerFsTools } from './tools/fs.js';
import { registerGitTools } from './tools/git.js';
import { registerExecTools } from './tools/exec.js';
export function registerAllTools(server) {
    registerFsTools(server);
    registerGitTools(server);
    registerExecTools(server);
}
//# sourceMappingURL=registry.js.map