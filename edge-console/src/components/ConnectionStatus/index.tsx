import React from "react";
import { Alert } from "antd";
import { WifiOutlined, DisconnectOutlined } from "@ant-design/icons";
import "./index.scss";

interface ConnectionStatusProps {
  appUrl: string
  isConnected: boolean;
  className?: string;
}

const ConnectionStatus: React.FC<ConnectionStatusProps> = ({
  appUrl,
  isConnected,
  className = "",
}) => {
  if (isConnected) {
    return null; // 连接正常时不显示
  }

  return (
    <div className={`connection-status ${className}`}>
      <Alert
        message="后端服务未连接"
        description={
          appUrl
            ? "部分功能可能不可用，请检查后端服务是否正常运行在 " + appUrl
            : "部分功能可能不可用，请检查后端服务是否正常运行"
        }
        type="warning"
        icon={<DisconnectOutlined />}
        showIcon
        banner
        closable
      />
    </div>
  );
};

export default ConnectionStatus;
