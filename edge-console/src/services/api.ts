import type { Task, GlobalConfig, ApiResponse } from "@/types";
import { io, Socket } from "socket.io-client";

// API基础配置
const API_BASE_URL =
  process.env.REACT_APP_API_BASE_URL || "http://localhost:9090";

export const SERVER_HOST = API_BASE_URL;

/**
 * 通用请求方法
 */
const apiRequest = async <T = any>(
  url: string,
  options: any = {}
): Promise<T | null> => {
  const { method = "GET", data, headers = {}, ...restOptions } = options;

  try {
    const fetchOptions: RequestInit = {
      method,
      headers: {
        "Content-Type": "application/json",
        ...headers,
      },
      timeout: 5000, // 5秒超时
      ...restOptions,
    };

    // 添加请求体
    if (data && (method === "POST" || method === "PUT" || method === "PATCH")) {
      if (typeof data === "string") {
        fetchOptions.body = data;
      } else {
        fetchOptions.body = JSON.stringify(data);
      }
    }

    const fullUrl = url.startsWith("http") ? url : `${API_BASE_URL}${url}`;

    // 使用 AbortController 实现超时
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5000);

    const response = await fetch(fullUrl, {
      ...fetchOptions,
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    // 检查响应是否为空
    const text = await response.text();
    if (!text) {
      return {} as T;
    }

    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  } catch (error: any) {
    console.warn(`API Request Failed for ${url}:`, error?.message || error);

    // 检查是否是网络连接错误
    if (error?.name === "AbortError") {
      console.warn("请求超时，请检查后端服务是否正常运行");
    } else if (error?.message?.includes("fetch")) {
      console.warn("无法连接到后端服务，请确认服务地址和状态");
    }

    return null; // 返回 null 而不是抛出错误
  }
};

/**
 * 视频流控制 API
 */
export class VideoApi {
  BASE_API_URL = "";
  constructor(API_URL?: string) {
    if (API_URL) {
      this.BASE_API_URL = API_URL;
    }
  }

  // 开始视频流
  startLiveVideoFeeding = () => {
    return apiRequest(this.BASE_API_URL + "/start", {
      method: "POST",
      data: {},
    });
  };

  // 停止视频流
  stopLiveVideoFeeding = () => {
    return apiRequest(this.BASE_API_URL + "/stop", {
      method: "POST",
      data: {},
    });
  };

  // 重启 RDK 推理服务
  restartRdk = () => {
    return apiRequest(this.BASE_API_URL + "/restart_rdk", {
      method: "POST",
      data: {},
    });
  };
}

/**
 * BOM相关 API
 */
export class BomApi {
  BASE_API_URL = "";
  constructor(API_URL?: string) {
    if (API_URL) {
      this.BASE_API_URL = API_URL;
    }
  }

  // 获取支持的BOM列表
  getBoms = () => {
    return apiRequest<ApiResponse<{ bom_en: string[]; bom_zh: string[] }>>(
      this.BASE_API_URL + "/supported_boms",
      {
        method: "GET",
      }
    );
  };
}

/**
 * 任务相关 API
 */
export class TaskApi {
  BASE_API_URL = "";
  constructor(API_URL?: string) {
    if (API_URL) {
      this.BASE_API_URL = API_URL;
    }
  }

  // 设置当前检测任务
  setCurDetectTask = (data: {
    name: string;
    detail: Record<string, number>;
    task_id: string;
  }) => {
    return apiRequest(this.BASE_API_URL + "/curr_task", {
      method: "POST",
      data,
    });
  };

  // 创建新任务
  createTask = (data: {
    name: string;
    task_detail: Record<string, number>;
  }) => {
    return apiRequest(this.BASE_API_URL + "/create", {
      method: "POST",
      data,
    });
  };

  // 查询所有任务
  queryTask = () => {
    return apiRequest<ApiResponse<Task[]>>(this.BASE_API_URL + "/query_task", {
      method: "GET",
    });
  };
}

/**
 * 全局配置 API
 */
export class ConfigApi {
  BASE_API_URL = "";
  constructor(API_URL?: string) {
    if (API_URL) {
      this.BASE_API_URL = API_URL;
    }
  }

  // 设置全局变量
  setGlobalVar = (data: { name: string; value: boolean }) => {
    return apiRequest("/set_global_var", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      data: JSON.stringify(data),
    });
  };

  // 获取全局配置
  getGlobalConfig = () => {
    return apiRequest<ApiResponse<GlobalConfig>>("/pull_config", {
      method: "GET",
    });
  };
}
export class SocketApi {
  BASE_API_URL = "";
  socket: Socket | null = null;
  constructor(API_URL?: string) {
    if (API_URL) {
      this.BASE_API_URL = API_URL;
    }
    this.init();
  }

  init = () => {
    this.socket = io(this.BASE_API_URL, {
      transports: ["websocket"],
      autoConnect: true,
    });
  };

  getSocket = () => {
    return this.socket;
  };
}
