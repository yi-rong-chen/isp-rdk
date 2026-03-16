// 全局类型声明文件

declare module '*.scss' {
  const content: Record<string, string>;
  export default content;
}

declare module '*.css' {
  const content: Record<string, string>;
  export default content;
}

declare module '*.less' {
  const content: Record<string, string>;
  export default content;
}

declare module '*.png' {
  const src: string;
  export default src;
}

declare module '*.jpg' {
  const src: string;
  export default src;
}

declare module '*.jpeg' {
  const src: string;
  export default src;
}

declare module '*.gif' {
  const src: string;
  export default src;
}

declare module '*.svg' {
  const src: string;
  export default src;
}

// Socket.IO 类型增强
declare module 'socket.io-client' {
  interface Socket {
    on(event: 'status', listener: (data: any) => void): this;
    on(event: 'alert', listener: (data: any) => void): this;
    on(event: 'change_task', listener: (data: any) => void): this;
    on(event: 'startup_check', listener: (data: any) => void): this;
  }
}

// 扩展 Window 对象
declare global {
  interface Window {
    // 可以在这里添加全局变量的类型声明
  }
}

export {}; 
