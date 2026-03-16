import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  useMemo,
} from "react";
import "./index.scss";
import LiveVideo from "@/components/LiveVideo";
import ConnectionStatus from "@/components/ConnectionStatus";
import { VideoApi, BomApi, TaskApi, ConfigApi } from "@/services/api";
import { throttle } from "@/utils";
// @ts-ignore
import io from "socket.io-client";
import type { Task } from "@/types";
import {
  Button,
  Divider,
  Select,
  Statistic,
  Row,
  Col,
  notification,
  Switch,
  Layout,
  Modal,
} from "antd";

const { Header, Content, Sider } = Layout;

// 验证 workstationId 是否为有效数字
const validateWorkstationId = (id: string | null): number | null => {
  if (!id) return null;
  const numId = Number(id);
  if (isNaN(numId) || !Number.isInteger(numId) || numId < 0) {
    console.warn(`[Config] 无效的 workstation_id: ${id}，必须是非负整数`);
    return null;
  }
  return numId;
};

// 从 URL 查询参数获取 workstation_id（必须是数字）
const getWorkstationIdFromUrl = (): number | null => {
  if (typeof window === 'undefined') return null;
  const params = new URLSearchParams(window.location.search);
  const id = params.get('workstation_id');
  return validateWorkstationId(id);
};

// 从环境变量获取 workstation_id（必须是数字）
const getWorkstationIdFromEnv = (): number | null => {
  const id = process.env.REACT_APP_WORKSTATION_ID || 
             process.env.UMI_APP_WORKSTATION_ID || 
             null;
  return validateWorkstationId(id);
};

// 获取后端配置（支持从外部传入）
const getHardcodedConfig = () => {
  // 优先级：URL 查询参数 > 环境变量 > 默认值（0）
  const workstationId = 
    getWorkstationIdFromUrl() ?? 
    getWorkstationIdFromEnv() ?? 
    0;

  // 根据 workstationId 计算端口
  // api_url 端口 = 9090 + workstationId
  const apiPort = 9090 + workstationId;

  return {
    workstation_id: `workstation-${workstationId}`,
    api_url: `http://localhost:${apiPort}`,
  };
};

// 硬编码的后端配置（动态获取）
const HARDCODED_CONFIG = getHardcodedConfig();

type StartupCheckPayload = {
  check_item?: string;
  status?: string;
  level?: string;
  message?: string;
  timestamp?: string;
  camera_name?: string;
  camera_host?: string;
};

// 输出配置信息到控制台（便于调试）
console.log('[Config] 后端配置:', HARDCODED_CONFIG);
console.log('[Config] workstation_id 来源:', 
  getWorkstationIdFromUrl() !== null ? 'URL 查询参数' : 
  getWorkstationIdFromEnv() !== null ? '环境变量' : 
  '默认值 (0)'
);

// 使用和原来完全一样的CCLayout组件
const CCLayout = ({ children, sider, uiInfo, className }: any) => {
  const { title = "项目名称" } = uiInfo || {};

  return (
    <Layout className={`cc-layout ${className || ""}`}>
      <Layout className="bottom-part">
        <Content className="cc-content">{children}</Content>
        {sider ? (
          <Sider width={"30%"} className="cc-sider">
            {sider}
          </Sider>
        ) : null}
      </Layout>
    </Layout>
  );
};

const VideoOperations = ({ feeding, startFeeding, stopFeeding }: any) => (
  <div className="video-operations">
    <Button
      danger={feeding}
      type="primary"
      size="large"
      onClick={feeding ? stopFeeding : startFeeding}
      style={
        !feeding ? { backgroundColor: "#52c41a", borderColor: "#52c41a" } : {}
      }
    >
      {feeding ? "停止检测" : "开始检测"}
    </Button>
  </div>
);

const DetectionStatus = ({ bomsStatus }: any) => (
  <div className="detection-status">
    {bomsStatus.map((status: any, index: number) => (
      <div
        key={index}
        className={`status-item ${status.status ? "success" : "failed"}`}
      >
        <span>{status.label}</span>
        <span
          className={`status-indicator ${status.status ? "success" : "failed"}`}
        />
      </div>
    ))}
  </div>
);

const NumberBomsSelect = ({ options, onChange, value }: any) => (
  <Select
    mode="multiple"
    style={{ width: "100%", marginBottom: 16 }}
    placeholder="选择BOM"
    value={value}
    onChange={onChange}
    options={options}
  />
);

const NumberBomsInput = ({ label, number, onChange, labelValue }: any) => (
  <div className="number-boms-input">
    <span>{label}:</span>
    <input
      type="number"
      min="1"
      value={number}
      onChange={(e) =>
        onChange({
          label,
          value: labelValue,
          number: parseInt(e.target.value) || 1,
        })
      }
    />
  </div>
);

const TaskInfo = ({ task, nameStrMap, taskStatus }: any) => {
  if (!task) return null;

  // 绿色和红色样式与检测状态一致
  const statusClass =
    taskStatus === true ? "success" : taskStatus === false ? "failed" : "";

  return (
    <div
      className={`task-info ${statusClass}`}
      style={{ marginTop: 8, padding: "8px 12px" }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 16px" }}>
        {Object.keys(task).map((key) => (
          <div
            key={key}
            style={{
              display: "flex",
              alignItems: "center",
              fontSize: 14,
              minWidth: 0,
            }}
          >
            <span style={{ color: "#bfbfbf", marginRight: 4 }}>
              {nameStrMap[key] || key}:
            </span>
            <span style={{ color: "#fff", fontWeight: 500 }}>{task[key]}</span>
          </div>
        ))}
      </div>
    </div>
  );
};


const _TAKS_KEY = "TASKS";
const _TASK_SELECT_KEY = "TASK_SELECT_KEY";

// 多物体装配状态展示组件
const MultiObjectAssemblyStatus = ({ 
  detailsList, 
  nameStrMap 
}: { 
  detailsList: any[]; 
  nameStrMap: Record<string, string>;
}) => {
  // 获取BOM键列表
  const bomKeys = useMemo(() => {
    if (!detailsList || detailsList.length === 0) return [];
    const firstDetails = detailsList[0];
    return Object.keys(firstDetails);
  }, [detailsList]);

  return (
    <div className="multi-object-assembly">
      {/* 装配个体卡片列表 */}
      <div className="assembly-cards">
        {detailsList.map((details, index) => (
          <div key={index} className="assembly-card">
            <div className="card-title">装配 {index + 1}</div>
            <div className="bom-letters">
              {bomKeys.map((key) => {
                const bomStatus = details[key]?.result || false;
                const bomName = nameStrMap[key] || key;
                return (
                  <div
                    key={key}
                    className={`letter-box ${bomStatus ? "success" : "failed"}`}
                    title={bomName}
                  >
                    {bomName}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
};

// 失败装配详情展示组件
const FailedAssemblyDetails = ({ 
  failedDetails, 
  nameStrMap 
}: { 
  failedDetails: Record<string, any>; 
  nameStrMap: Record<string, string>;
}) => {
  if (!failedDetails || Object.keys(failedDetails).length === 0) {
    return null;
  }

  return (
    <div className="failed-assembly-details">
      <div className="failed-title">⚠️ 检测失败项</div>
      <div className="failed-items">
        {Object.entries(failedDetails).map(([key, value]) => (
          <div key={key} className="failed-item">
            <span className="item-name">{nameStrMap[key] || key}</span>
            <span className="item-value">
              {typeof value === 'object' ? JSON.stringify(value) : String(value)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
};

// 检测历史记录展示组件
const DetectionHistory = ({ 
  history, 
  nameStrMap 
}: { 
  history: Array<{
    id: string;
    timestamp: string;
    result: boolean;
    type: 'normal' | 'track';
    failedItems?: string[];
  }>; 
  nameStrMap: Record<string, string>;
}) => {
  if (!history || history.length === 0) {
    return (
      <div className="detection-history-empty">
        <span>暂无检测记录</span>
      </div>
    );
  }

  return (
    <div className="detection-history-list">
      {history.map((record) => (
        <div 
          key={record.id} 
          className={`history-item ${record.result ? 'success' : 'failed'}`}
        >
          <div className="history-time">{record.timestamp}</div>
          <div className={`history-status ${record.result ? 'success' : 'failed'}`}>
            {record.result ? 'OK' : 'NG'}
          </div>
          
          {/* 只有 NG 时才显示失败的 BOM 项 */}
          {!record.result && record.failedItems && record.failedItems.length > 0 && (
            <div className="history-failed-items">
              {record.failedItems.map((item, idx) => (
                <span key={idx} className="failed-badge">
                  {item}
                </span>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
};

export default function IndexPage() {
  const { workstation_id, api_url } = HARDCODED_CONFIG;
  const [modelStreaming, setModelStreaming] = useState(false);
  const [streamUrl, setStreamUrl] = useState<string | undefined>(undefined);
  const [bomsOptions, setOptions] = useState<
    { label: string; value: string; number: number }[]
  >([]);
  const [selectedBoms, setSelectedBoms] = useState<
    { number: number; label: string; value: string }[]
  >([]);
  const [allTasks, setAllTasks] = useState<Task[]>([]);
  const [curTask, setCurTask] = useState<Task["detail"]>();
  const [currentTaskId, setCurrentTaskId] = useState<string>();
  const [currentTaskName, setCurrentTaskName] = useState<string>();
  const [totalTaskStatus, setTotalTaskStatus] = useState<boolean>(false);
  const [curTaskStatus, setCurTaskStatus] = useState<
    { label: string; status: boolean }[]
  >([]);
  const [counting, setCounting] = useState(0);
  const [failedCounting, setFailedCounting] = useState(0);
  const [ttTime, setTtTime] = useState<number | null>(null);
  const [isConnected, setIsConnected] = useState<boolean>(true);
  const nameStrMap = useRef<Record<string, string>>({});
  const numberBomsSelectRef = useRef<any>(null);
  const [lightFlag, setLightFlag] = useState<boolean | null>(null);
  const [reinitTrigger, setReinitTrigger] = useState<number>(0); // 用于触发重新初始化
  const connectionCheckIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const videoApi = useRef(new VideoApi(api_url));
  const taskApi = useRef(new TaskApi(api_url));
  const configApi = useRef(new ConfigApi(api_url));
  const bomApi = useRef(new BomApi(api_url));
  
  // 多物体装配相关状态
  const [trackDetails, setTrackDetails] = useState<{
    details_list: any[];
    failed_details: Record<string, any>;
  } | null>(null);
  const [failedDetails, setFailedDetails] = useState<Record<string, any>>({});
  const failedDetailsTimerRef = useRef<NodeJS.Timeout | null>(null);

  // 检测历史记录（最多保留10条）
  const [detectionHistory, setDetectionHistory] = useState<Array<{
    id: string;
    timestamp: string;
    result: boolean;
    type: 'normal' | 'track'; // 区分普通检测和多物体装配
    failedItems?: string[]; // 失败的 BOM 项名称列表
  }>>([]);

  // 用于跟踪上一次的计数，判断是否有新检测
  const prevCountingRef = useRef<number>(0);
  const prevFailedCountingRef = useRef<number>(0);
  const isFirstStatusRef = useRef<boolean>(true);
  const startupCheckNoticeCacheRef = useRef<Set<string>>(new Set());

  // 检查后端连接状态
  const checkConnection = async () => {
    if (!api_url) {
      console.warn("api_url 不存在，无法检查连接状态");
      setIsConnected(false);
      return false;
    }

    try {
      const response = await fetch(`${api_url}/check_ready`, {
        method: "GET",
        signal: AbortSignal.timeout(3000), // 3秒超时
      });
      const isHealthy = response.ok;
      setIsConnected(isHealthy);

      if (!isHealthy) {
        console.warn("后端服务连接异常");
        return false;
      }
      return true;
    } catch (error) {
      console.warn("连接检查失败:", error);
      setIsConnected(false);
      return false;
    }
  };

  // 视频流错误处理
  const handleStreamError = useCallback(() => {
    console.error("视频流出现错误，交由LiveVideo组件处理重连机制");
  }, []);

  const startFeeding = async (retryCount = 0, maxRetries = 3) => {
    if (!api_url) {
      console.warn("api_url 不存在，无法启动视频流");
      notification.error({
        message: "启动视频流失败",
        description: "设备信息不完整，请刷新页面重试",
      });
      return;
    }

    try {
      console.log(`[LiveVideo] 尝试启动视频流 (第 ${retryCount + 1} 次)...`);
      const res: any = await videoApi.current.startLiveVideoFeeding();
      if (!res || (res as any).code !== 200) {
        throw new Error((res as any)?.message || "启动失败");
      }
      const url =
        (res as any).stream_url ||
        (api_url ? `http://${new URL(api_url).hostname}:5050/stream` : undefined);
      if (!url) {
        throw new Error("响应中未包含 stream_url");
      }
      setStreamUrl(url);
      setModelStreaming(true);
      console.log("[LiveVideo] 视频流启动成功，stream_url:", url);
    } catch (error) {
      console.warn(`[LiveVideo] 第 ${retryCount + 1} 次启动视频流失败:`, error);
      
      // 如果还有重试次数，等待后重试
      if (retryCount < maxRetries) {
        const retryDelay = 2000 * (retryCount + 1); // 递增延迟: 2s, 4s, 6s
        console.log(`[LiveVideo] 将在 ${retryDelay}ms 后重试...`);
        notification.warning({
          message: "视频流启动失败",
          description: `RDK 推理服务可能未准备好，${retryDelay / 1000} 秒后自动重试 (${retryCount + 1}/${maxRetries})...`,
          duration: 2,
        });
        setTimeout(() => {
          startFeeding(retryCount + 1, maxRetries);
        }, retryDelay);
      } else {
        // 已达到最大重试次数
        notification.error({
          message: "启动视频流失败",
          description: `已重试 ${maxRetries} 次，请检查后端服务是否正常运行，或稍后手动启动`,
          duration: 5,
        });
      }
    }
  };

  const stopFeeding = async () => {
    if (!api_url) {
      console.warn("api_url 不存在，无法停止视频流");
      setModelStreaming(false);
      setStreamUrl(undefined);
      return;
    }

    try {
      await videoApi.current.stopLiveVideoFeeding();
      setModelStreaming(false);
      setStreamUrl(undefined);
    } catch (error) {
      console.warn("停止视频流失败:", error);
      setModelStreaming(false);
      setStreamUrl(undefined);
    }
  };

  const fetchAllTask = async () => {
    if (!api_url) {
      console.warn("api_url 不存在，无法获取任务");
      return false;
    }

    try {
      let _tasks = await taskApi.current.queryTask();
      console.log(_tasks);
      if (_tasks != undefined && _tasks != null && _tasks.data) {
        setAllTasks(_tasks.data);
        localStorage.setItem(_TAKS_KEY, JSON.stringify(_tasks.data));
        let taskId = (_tasks as any).task_id;
        console.log("#TASK ID: ", taskId);
        if (!taskId || taskId === null || taskId === undefined) {
          // 如果没有有效的task_id，则不进行设置，直接返回false
          console.log("#NO DEFAULT TASK.");
          return false;
        }
        console.log("#SET DEFAULT TASK: ", taskId);
        setCurrentTaskId(taskId);
        localStorage.setItem(_TASK_SELECT_KEY, taskId);
        return true;
      }
    } catch (error) {
      console.error("获取任务时出错:", error);
      return false;
    }
  };

  const setDefaultTaskAndStart = () => {
    selectDefaultTask();
    startFeeding();
  };

  const selectDefaultTask = () => {
    let HISTORY_TASKS = JSON.parse(localStorage.getItem(_TAKS_KEY) || "[]");
    let HISTORY_TASK_KEY = localStorage.getItem(_TASK_SELECT_KEY);
    if (HISTORY_TASKS.length < 1 || !HISTORY_TASK_KEY) {
      console.log("#NO DEFAULT TASK.");
      return;
    }
    HISTORY_TASKS.find((value: any) => {
      if (value.task_id === HISTORY_TASK_KEY) {
        console.log(value);
        console.log("# SET DEFAULT TASK: ", HISTORY_TASK_KEY);
        setCurrentTaskName(value.name);
        onSelectTask(value.name, {
          label: value.name,
          value: value.name, // 使用任务名称作为 value
          id: value.task_id,
          detail: value.detail, // 直接传递任务详情对象
        });
        return true;
      }
      return false;
    });
  };

  // 优化的 processStatus 函数 - 避免不必要的状态更新
  const processStatus = useCallback(
    (data: {
      details: any;
      result: boolean;
      total_counting: number;
      total_failed_counting: number;
      track_details?: {
        details_list: any[];
        failed_details: Record<string, any>;
      };
    }) => {

      // 检查数据完整性
      if (!data || typeof data !== 'object') {
        console.error("[ProcessStatus] 数据格式错误，data不是对象:", data);
        return;
      }

      // 处理多物体装配场景
      if (data.track_details) {
        console.log("[ProcessStatus] 检测到 track_details，启用多物体装配模式");
        
        // 更新 track_details
        setTrackDetails({
          details_list: data.track_details.details_list || [],
          failed_details: data.track_details.failed_details || {},
        });

        // 处理失败详情 - 如果不为空则展示5秒
        if (data.track_details.failed_details && 
            Object.keys(data.track_details.failed_details).length > 0) {
          console.log("[ProcessStatus] 检测到失败装配详情:", data.track_details.failed_details);
          
          // 清除之前的定时器
          if (failedDetailsTimerRef.current) {
            clearTimeout(failedDetailsTimerRef.current);
          }
          
          // 设置失败详情
          setFailedDetails(data.track_details.failed_details);
          
          // 5秒后清除
          failedDetailsTimerRef.current = setTimeout(() => {
            console.log("[ProcessStatus] 清除失败装配详情");
            setFailedDetails({});
          }, 5000);
        }
      } else {
        // 没有 track_details，清除相关状态
        setTrackDetails(null);
        
        if (!data.details || typeof data.details !== 'object') {
          console.warn("[ProcessStatus] details字段缺失或格式错误");
        }

        // 处理任务状态
        const status = data.details ? Object.keys(data.details).map((key) => ({
          label: nameStrMap.current[key] || key,
          content: data.details[key]?.value,
          status: data.details[key]?.result,
        })) : [];

        // 只有在真正发生变化时才更新状态，避免不必要的重渲染
        setCurTaskStatus((prev) => {
          const hasChanged = JSON.stringify(prev) !== JSON.stringify(status);
          if (!hasChanged) {
            return prev;
          }
          return status;
        });
      }

      setTotalTaskStatus((prev) => {
        const newStatus = data?.result ?? false;
        if (prev === newStatus) {
          console.log("[ProcessStatus] 总体状态无变化，跳过更新");
          return prev;
        }
        console.log("[ProcessStatus] 总体状态已更新:", newStatus);
        return newStatus;
      });

      // 判断是否有新检测（通过计数变化判断）
      // total_counting 是总检测数（OK+NG），total_failed_counting 是 NG 数
      const newTotalCount = typeof data.total_counting === 'number' ? data.total_counting : 0;
      const newNgCount = typeof data.total_failed_counting === 'number' ? data.total_failed_counting : 0;
      const newOkCount = newTotalCount - newNgCount;
      
      let hasNewDetection = false;
      let isOkDetection = false;

      // 检查实际 OK 计数是否增加（实际OK = 总数 - NG数）
      if (newOkCount > prevCountingRef.current) {
        hasNewDetection = true;
        isOkDetection = true;
        console.log("[ProcessStatus] 检测到新的 OK 记录");
      }
      
      // 检查 NG 计数是否增加
      if (newNgCount > prevFailedCountingRef.current) {
        hasNewDetection = true;
        isOkDetection = false;
        console.log("[ProcessStatus] 检测到新的 NG 记录");
      }

      // 更新显示计数（OK 显示实际 OK 数，不是总数）
      setCounting(newOkCount);
      setFailedCounting(newNgCount);
      
      // 首次收到状态数据时，仅同步计数，不添加历史记录
      if (isFirstStatusRef.current) {
        prevCountingRef.current = newOkCount;
        prevFailedCountingRef.current = newNgCount;
        isFirstStatusRef.current = false;
        console.log("[ProcessStatus] 首次数据同步，跳过历史记录", { ok: newOkCount, ng: newNgCount });
        return;
      }

      // 更新引用值（保存实际 OK 数和 NG 数）
      prevCountingRef.current = newOkCount;
      prevFailedCountingRef.current = newNgCount;

      // 只有当检测计数增加时才记录历史
      if (hasNewDetection) {
        // 提取失败的 BOM 项
        const failedItems: string[] = [];
        
        if (data.track_details) {
          // 多物体装配模式：从 failed_details 中提取
          if (data.track_details.failed_details) {
            Object.keys(data.track_details.failed_details).forEach(key => {
              failedItems.push(nameStrMap.current[key] || key);
            });
          }
          // 如果 failed_details 为空，从 details_list 中找失败项
          if (failedItems.length === 0 && data.track_details.details_list) {
            const allKeys = new Set<string>();
            data.track_details.details_list.forEach(details => {
              Object.entries(details).forEach(([key, value]: [string, any]) => {
                if (!value?.result) {
                  allKeys.add(nameStrMap.current[key] || key);
                }
              });
            });
            failedItems.push(...Array.from(allKeys));
          }
        } else if (data.details) {
          // 普通检测模式：从 details 中提取失败项
          Object.entries(data.details).forEach(([key, value]: [string, any]) => {
            if (!value?.result) {
              failedItems.push(nameStrMap.current[key] || key);
            }
          });
        }

        const newRecord = {
          id: Date.now().toString(),
          timestamp: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
          result: isOkDetection,
          type: (data.track_details ? 'track' : 'normal') as 'normal' | 'track',
          failedItems: failedItems,
        };

        setDetectionHistory((prev) => {
          const updated = [newRecord, ...prev].slice(0, 10);
          console.log("[DetectionHistory] 添加新检测记录:", {
            result: newRecord.result ? 'OK' : 'NG',
            type: newRecord.type,
            failedItems: newRecord.failedItems,
          });
          return updated;
        });
      }
    },
    []
  );

  useEffect(() => {
    // 如果 api_url 不存在，不执行 socket 连接
    if (!api_url) {
      console.log("api_url 不存在，跳过 socket 连接");
      return;
    }
    const socket = io(api_url, {
      transports: ["websocket"],
    });

    const processAlert = throttle((data: { result: boolean }) => {
      if (data.result) {
        return;
      }
      notification.error({
        message: "One Detection Task Failed!",
        style: {
          backgroundColor: "#5c0011",
        },
      });
    }, 5000);

    const changeTask = throttle((data: { task_id: string }) => {
      let HISTORY_TASKS = JSON.parse(localStorage.getItem(_TAKS_KEY) || "[]");
      HISTORY_TASKS.find((value: any) => {
        if (value.task_id === data.task_id) {
          console.log(value);
          console.log("# SET DEFAULT TASK: ", data.task_id);
          setCurrentTaskName(value.name);
          onSelectTask(value.name, {
            label: value.name,
            value: value.name, // 使用任务名称作为 value
            id: value.task_id,
            detail: value.detail, // 直接传递任务详情对象
          });
          return true;
        }
        return false;
      });
    }, 5000);

    const processStartupCheck = (data: StartupCheckPayload) => {
      if (!data || typeof data !== "object") {
        console.warn("[StartupCheck] 无效数据:", data);
        return;
      }

      const checkItem = data.check_item || "startup_check";
      const message = data.message || "服务启动自检失败";
      const dedupeKey = `${checkItem}|${message}`;
      if (startupCheckNoticeCacheRef.current.has(dedupeKey)) {
        return;
      }
      startupCheckNoticeCacheRef.current.add(dedupeKey);

      const titleMap: Record<string, string> = {
        device_authorization: "设备授权检查失败",
        camera_ping: "相机连通性检查失败",
        startup_self_check: "服务启动自检异常",
      };

      const openModal =
        data.level === "error"
          ? Modal.error
          : data.level === "success"
            ? Modal.success
            : data.level === "info"
              ? Modal.info
              : Modal.warning;

      const clearDedupe = () => {
        startupCheckNoticeCacheRef.current.delete(dedupeKey);
      };

      openModal({
        title: titleMap[checkItem] || "服务启动自检提示",
        content: message,
        centered: true,
        closable: true,
        maskClosable: false,
        okText: "知道了",
        onOk: clearDedupe,
        onCancel: clearDedupe,
      });
    };

    socket.on("status", processStatus);
    
    // 监听 TT 时间事件
    socket.on("tt", (tt: number) => {
      console.log("[TT] 收到 TT 时间:", tt);
      setTtTime(tt);
    });

    // 监听 start 事件，收到后重新初始化页面
    socket.on("start", (data: any) => {
      console.log("[Start] 收到 start 事件，触发重新初始化:", data);
      // 触发重新初始化
      setReinitTrigger(prev => prev + 1);
    });

    socket.on("startup_check", processStartupCheck);

    return () => {
      socket.disconnect();
    };
  }, [processStatus, api_url]);

  // 清理失败详情定时器
  useEffect(() => {
    return () => {
      if (failedDetailsTimerRef.current) {
        clearTimeout(failedDetailsTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    // 如果 api_url 不存在，不执行初始化
    if (!api_url) {
      console.log("api_url 不存在，跳过系统初始化");
      return;
    }

    console.log("[初始化] 开始系统初始化，触发器值:", reinitTrigger);

    // 重置状态（当重新初始化时）
    if (reinitTrigger > 0) {
      console.log("[初始化] 重置所有状态");
      setCounting(0);
      setFailedCounting(0);
      setTtTime(null);
      setCurTaskStatus([]);
      setTotalTaskStatus(false);
      isFirstStatusRef.current = true;
      // 停止当前视频流
      if (modelStreaming) {
        stopFeeding();
      }
    }

    // 初始化系统
    const initializeSystem = async () => {
      // 首次检查连接
      const isConnected = await checkConnection();
      if (!isConnected) {
        console.warn("系统初始化失败：后端服务未就绪");
        notification.warning({
          message: "系统初始化失败",
          description: "后端服务未就绪，等待重新连接...",
          duration: 3,
        });
        return;
      }

      // 获取全局配置
      try {
        const config = await configApi.current.getGlobalConfig();
        if (config) {
          setLightFlag(config.data.LIGHT_FLAG);
        }
      } catch (error) {
        console.error("获取全局配置失败:", error);
        notification.error({
          message: "获取配置失败",
          description: "无法获取系统配置，使用默认值",
        });
      }

      // 获取BOM列表
      try {
        const res = await bomApi.current.getBoms();
        if (res) {
          const boms = res.data.bom_zh;
          const bomsEn = res.data.bom_en;
          const map: Record<string, string> = {};

          const opts = boms.map((item, index) => {
            map[bomsEn[index]] = item;
            return {
              label: item,
              value: bomsEn[index],
              number: 1,
            };
          });
          nameStrMap.current = map;
          setOptions(opts);
        }
      } catch (error) {
        console.error("获取BOM列表失败:", error);
      }

      // 获取并设置任务
      const taskResult = await fetchAllTask();
      if (taskResult) {
        // 收到 start 事件表示 RDK 推理服务需要重新初始化
        // 增加等待时间，确保 RDK 推理服务完全准备好
        selectDefaultTask();
        const waitTime = reinitTrigger > 0 ? 5000 : 1000;
        console.log(`[初始化] 等待 ${waitTime}ms 后启动视频流...`);
        setTimeout(() => {
          setDefaultTaskAndStart();
        }, waitTime);
      } else {
        // 如果没有任务结果，也应尝试设置默认任务（如果有存储的）
        selectDefaultTask();
      }
    };

    // 持续轮询检查 checkConnection，直到返回 true 后调用 initializeSystem，只在初始化时执行一次
    const pollConnection = async () => {
      while (true) {
        const isConnected = await checkConnection();
        if (isConnected) {
          initializeSystem();
          break;
        }
        // 每隔1秒重试一次
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    };
    pollConnection();

    return () => {
      if (connectionCheckIntervalRef.current) {
        clearInterval(connectionCheckIntervalRef.current);
      }
    };
  }, [api_url, reinitTrigger]);

  const onBomsChange = (value: any, option: any) => {
    // 确保 option 是数组
    const options = Array.isArray(option) ? option : [option];

    const selectedData = options.map((item: any) => {
      const bomTarget = selectedBoms.find((bom) => bom.value === item.value);
      return {
        ...item,
        number: bomTarget?.number || 0,
      };
    });
    setSelectedBoms(selectedData);
  };

  const onBomsNumber = (res: {
    number: number;
    label: string;
    value: string;
  }) => {
    selectedBoms.forEach((bom) => {
      if (bom.value === res.value) {
        bom.number = res.number;
      }
    });
    setSelectedBoms([...selectedBoms]); // 创建新数组触发重新渲染
  };

  const onSelectTask = (value: string, option?: any) => {
    if (!api_url) {
      console.warn("api_url 不存在，无法设置任务");
      return;
    }

    // 从 localStorage 中获取任务列表，根据 value（任务名称）查找对应的任务
    let _tasks = JSON.parse(localStorage.getItem(_TAKS_KEY) || "[]");
    const selectedTask = _tasks.find((task: any) => task.name === value);
    
    if (!selectedTask) {
      console.warn("未找到对应的任务:", value);
      return;
    }

    // 如果 option 存在（从 Select 的 onSelect 事件），优先使用 option 中的数据
    let taskDetail, taskId, taskName;
    if (option && option.id) {
      // option.detail 是任务详情对象，option.value 现在是任务名称
      taskDetail = option.detail || selectedTask.detail;
      taskId = option.id;
      taskName = option.label || value;
    } else {
      // 否则从 selectedTask 中获取
      taskDetail = selectedTask.detail;
      taskId = selectedTask.task_id;
      taskName = selectedTask.name;
    }

    setCurTask(taskDetail);
    setCurrentTaskId(taskId);
    setCurrentTaskName(taskName);

    // 切换任务时立即重置检测状态，避免显示旧任务的状态
    if (taskDetail) {
      const resetStatus = Object.keys(taskDetail).map(key => ({
        label: nameStrMap.current[key] || key,
        status: false,
      }));
      setCurTaskStatus(resetStatus);
    } else {
      setCurTaskStatus([]);
    }
    setTotalTaskStatus(false);
    setTrackDetails(null);

    taskApi.current.setCurDetectTask({
      name: taskName,
      detail: taskDetail,
      task_id: taskId,
    });
  };

  const addNewTask = async () => {
    if (!api_url) {
      console.warn("api_url 不存在，无法创建任务");
      notification.error({
        message: "创建失败",
        description: "设备信息不完整，请刷新页面重试",
      });
      return;
    }

    const detail: Record<string, number> = {};
    let name = "";
    selectedBoms.forEach(
      (item: { number: number; label: string; value: string }) => {
        detail[item.value] = item.number;
        name += `${item.label}:${item.number},`;
      }
    );

    name = name.slice(0, -1);

    let _tasks = allTasks;
    const taskExists = _tasks.some((task) => task.name === name);

    if (taskExists) {
      notification.error({
        message: "创建失败",
        description: "该任务已存在",
      });
      return;
    }

    // 删除多余的localStorage保存，fetchAllTask会处理
    // localStorage.setItem(_TAKS_KEY, JSON.stringify(_tasks));

    try {
      let res = await taskApi.current.createTask({
        name: name,
        task_detail: detail as any,
      });
      if (!res) {
        notification.error({
          message: "创建失败",
          description: "新任务创建失败",
        });
        return;
      }
    } catch (error) {
      console.error("获取任务时出错:", error);
    }

    // 重新获取任务列表，这会更新localStorage和state
    await fetchAllTask();

    setSelectedBoms([]);

    if (
      numberBomsSelectRef.current &&
      typeof numberBomsSelectRef.current.reset === "function"
    ) {
      numberBomsSelectRef.current.reset();
    }

    notification.success({
      message: "创建成功",
      description: "新任务已成功创建",
    });
  };

  const updateGlobalVar = async (name: string, value: boolean) => {
    if (!api_url) {
      console.warn("api_url 不存在，无法更新全局变量");
      notification.error({
        message: "更新失败",
        description: "设备信息不完整，请刷新页面重试",
      });
      return;
    }

    try {
      const response = await configApi.current.setGlobalVar({
        name: name,
        value: value,
      });

      if (response && response.code != 200) {
        throw new Error("更新失败");
      }
      if (name == "LIGHT_FLAG") {
        if (value) {
          notification.success({
            message: "更新成功",
            description: `三色灯已打开`,
          });
        } else {
          notification.success({
            message: "更新成功",
            description: `三色灯已关闭`,
          });
        }
      }
    } catch (error: any) {
      notification.error({
        message: "更新失败",
        description: error.message,
      });
    }
  };

  const renderSider = () => {
    let _tasks = JSON.parse(localStorage.getItem(_TAKS_KEY) || "[]");
    
    // 根据task_id去重，避免下拉列表出现重复任务
    const uniqueTasks = _tasks.reduce((acc: any[], item: any) => {
      if (!acc.find((t: any) => t.task_id === item.task_id)) {
        acc.push(item);
      }
      return acc;
    }, []);
    
    const allTasksOptions = uniqueTasks.map((item: any) => ({
      label: item.name,
      value: item.name, // 使用任务名称作为 value，与 currentTaskName 匹配
      id: item.task_id,
      key: item.task_id, // 添加唯一key
      detail: item.detail, // 保存任务详情，方便后续使用
    }));

    return (
      <>
        <Row gutter={16} style={{ marginBottom: 16 }}>
          <Col span={24}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <h3
                style={{
                  fontSize: "16px",
                  marginBottom: 0,
                  whiteSpace: "nowrap",
                }}
              >
                三色灯
              </h3>
              <Switch
                checked={lightFlag === true}
                onChange={async (checked) => {
                  setLightFlag(checked);
                  await updateGlobalVar("LIGHT_FLAG", checked);
                }}
                checkedChildren="开启"
                unCheckedChildren="关闭"
                size="default"
              />
            </div>
          </Col>
        </Row>

        <h3>统计</h3>
        <Row gutter={16}>
          <Col span={8}>
            <Statistic
              title="OK"
              value={counting}
              valueStyle={{ color: "#3f8600" }}
            />
          </Col>
          <Col span={8}>
            <Statistic
              title="NG"
              value={failedCounting}
              valueStyle={{ color: "#cf1322" }}
            />
          </Col>
          <Col span={8}>
            <Statistic
              title="TT时间"
              value={ttTime !== null ? ttTime : 0}
              suffix="秒"
              valueStyle={{ color: "#1890ff" }}
            />
          </Col>
        </Row>
        <Divider />

        <h3>当前任务</h3>
        <Select
          size="large"
          placeholder="选择任务"
          style={{ width: "100%" }}
          options={allTasksOptions}
          onSelect={(value, option) => {
            // 使用 onSelect 事件，可以获取完整的 option 对象
            onSelectTask(value as string, option);
          }}
          value={currentTaskName}
        />
        {curTask ? (
          <TaskInfo
            task={curTask}
            taskStatus={totalTaskStatus}
            nameStrMap={nameStrMap.current}
          />
        ) : null}
        <Divider />
        
        {/* 失败装配详情展示区域 */}
        {failedDetails && Object.keys(failedDetails).length > 0 && (
          <>
            <FailedAssemblyDetails 
              failedDetails={failedDetails}
              nameStrMap={nameStrMap.current}
            />
            <Divider />
          </>
        )}
        
        <h3>检测状态</h3>
        {/* 根据是否有 track_details 决定展示方式 */}
        {trackDetails && trackDetails.details_list.length > 0 ? (
          <MultiObjectAssemblyStatus 
            detailsList={trackDetails.details_list}
            nameStrMap={nameStrMap.current}
          />
        ) : (
          <DetectionStatus bomsStatus={curTaskStatus} />
        )}
      </>
    );
  };

  return (
    <CCLayout
      uiInfo={{
        title: "AI检测",
      }}
      sider={renderSider()}
    >
      <div className="main-content-wrapper">
        {/* 视频区域 */}
        <div className="video-section">
          <ConnectionStatus isConnected={isConnected} appUrl={api_url || ""} />
          <LiveVideo
            key={workstation_id}
            appUrl={api_url || ""}
            streaming={modelStreaming}
            streamUrl={streamUrl}
            onStreamError={handleStreamError}
            onStopFeeding={stopFeeding}
            onStartFeeding={startFeeding}
          />
        </div>

        {/* 底部历史记录区域 */}
        <div className="history-section">
          <div className="history-header">
            <h3>检测历史记录</h3>
            <div className="history-controls">
              <Button
                danger={modelStreaming}
                type="primary"
                size="large"
                onClick={() => modelStreaming ? stopFeeding() : startFeeding()}
                style={
                  !modelStreaming ? { backgroundColor: "#52c41a", borderColor: "#52c41a" } : {}
                }
              >
                {modelStreaming ? "停止检测" : "开始检测"}
              </Button>
            </div>
          </div>
          <DetectionHistory 
            history={detectionHistory} 
            nameStrMap={nameStrMap.current}
          />
        </div>
      </div>
    </CCLayout>
  );
}
