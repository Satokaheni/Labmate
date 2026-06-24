import path from 'path';
import fs from 'fs';

interface ToolRequest {
  tool: string;
  params: Record<string, any>;
}

interface ToolResult {
  success: boolean;
  data?: any;
  error?: string;
}

export function validateToolPath(toolPath: string): boolean {
  // Ensure the path is within allowed directories
  const realPath = path.resolve(toolPath);
  const allowedDirs = [
    path.resolve(process.cwd()),
  ];

  return allowedDirs.some(dir => realPath.startsWith(dir));
}

export async function executeTool(request: ToolRequest): Promise<ToolResult> {
  try {
    if (request.tool === 'readFile') {
      const filePath = request.params.path as string;
      if (!validateToolPath(filePath)) {
        return {
          success: false,
          error: 'Path not allowed',
        };
      }

      const content = fs.readFileSync(filePath, 'utf-8');
      return {
        success: true,
        data: content,
      };
    }

    return {
      success: false,
      error: 'Unknown tool',
    };
  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error',
    };
  }
}
