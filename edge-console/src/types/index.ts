/** 任务类型定义 */
export type Task = {
  name: string;
  detail: Record<string, number>;
  task_id: string;
};

/** 检测状态 */
export type DetectionStatus = {
  label: string;
  content?: any;
  status: boolean;
};

/** BOM选项 */
export type BomOption = {
  label: string;
  value: string;
  number: number;
};

/** 全局配置 */
export type GlobalConfig = {
  DEV_FLAG: boolean;
  LIGHT_FLAG: boolean;
};

/** API响应基础类型 */
export type ApiResponse<T = any> = {
  code: number;
  data: T;
  message?: string;
};

/** Socket状态数据 */
export type SocketStatusData = {
  details: Record<string, { value: any; result: boolean }>;
  result: boolean;
  total_counting: number;
  total_failed_counting: number;
}; 