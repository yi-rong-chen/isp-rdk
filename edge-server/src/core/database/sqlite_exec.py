import sqlite3
import json
from contextlib import contextmanager
import queue
import threading
import src.core.config.global_var as g

# 全局连接池实例
_db_pool = None
_pool_lock = threading.Lock()

class DBPool:
    def __init__(self, db_path, max_connections=3):
        self.db_path = db_path
        self.pool = queue.Queue(maxsize=max_connections)
        # 创建连接池
        for _ in range(max_connections):
            conn = sqlite3.connect(db_path, check_same_thread=False)
            # 设置连接属性
            conn.row_factory = sqlite3.Row  # 使查询结果更易于使用
            self.pool.put(conn)
    
    @contextmanager
    def get_connection(self):
        conn = self.pool.get()
        try:
            yield conn
        except Exception as e:
            conn.rollback()  # 出错时回滚
            raise e
        finally:
            self.pool.put(conn)
    
    def close_all(self):
        """关闭所有连接"""
        while not self.pool.empty():
            try:
                conn = self.pool.get_nowait()
                conn.close()
            except queue.Empty:
                break

def get_db_pool():
    """获取数据库连接池单例"""
    global _db_pool
    if _db_pool is None:
        with _pool_lock:
            if _db_pool is None:
                _db_pool = DBPool('isp.db', max_connections=3)
    return _db_pool

def init_sqlite():
    create_db()
    clean_expired_data()

def create_db():
    """创建数据库表"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()

        # 创建tasks表（如果不存在）
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 创建ng表（如果不存在）
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS ng (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 创建count表（如果不存在）- 用于存储每个型号每天的生产统计
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS count (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            production_date DATE NOT NULL,
            total_count INTEGER DEFAULT 0,
            ng_count INTEGER DEFAULT 0,
            UNIQUE(task_id, production_date)
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS current_task_id (
            current_task_id VARCHAR(255) NOT NULL
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks_content (
            tasks_content TEXT NOT NULL
        )
        ''')
            
        conn.commit()
    
def write_to_db(db_name, data):
    """写入数据到指定表"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        
        # 将字典转换为JSON字符串
        task_json = json.dumps(data)
        
        # 插入数据
        cursor.execute(f'INSERT INTO {db_name} (data) VALUES (?)', (task_json,))
        conn.commit()

def read_min_id(db_name):
    """读取指定表中最小ID的数据"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f'SELECT data FROM {db_name} ORDER BY id ASC LIMIT 1')
        min_data = cursor.fetchone()
        
        if min_data:
            return json.loads(min_data[0])
        return None

def delete_min_id(db_name):
    """删除指定表中最小ID的记录"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute(f'SELECT id FROM {db_name} ORDER BY id ASC LIMIT 1')
        min_id = cursor.fetchone()
        
        if min_id:
            cursor.execute(f'DELETE FROM {db_name} WHERE id = ?', (min_id[0],))
            conn.commit()

def clean_expired_data():
    """清理过期的生产数据，只保留当天的数据"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        
        # 获取当前日期
        from datetime import date
        today = date.today().strftime('%Y-%m-%d')
        
        try:
            # 删除非当天的count表数据
            cursor.execute('''
            DELETE FROM count WHERE production_date != ?
            ''', (today,))
            
            deleted_count = cursor.rowcount
            g.logger.info(f"清理了 {deleted_count} 条过期的生产数据")
            
            conn.commit()
            
        except Exception as e:
            g.logger.error(f"清理过期数据时发生错误: {e}")
            raise e

def update_count_data(task_id, total_count, ng_count):
    """更新当天的生产数据"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        
        # 获取当前日期
        from datetime import date
        today = date.today().strftime('%Y-%m-%d')
        
        try:
            # 使用INSERT OR REPLACE来插入或更新数据
            cursor.execute('''
            INSERT OR REPLACE INTO count (task_id, production_date, total_count, ng_count)
            VALUES (?, ?, ?, ?)
            ''', (task_id, today, total_count, ng_count))
            
            conn.commit()
            g.logger.info(f"更新任务 {task_id} 当天数据: 总数={total_count}, NG数={ng_count}")
            
        except Exception as e:
            g.logger.error(f"更新生产数据时发生错误: {e}")
            raise e
    
def read_count_by_task_id(current_status, task_id):
    """根据任务ID读取统计数据"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        
        # 获取当前日期
        from datetime import date
        today = date.today().strftime('%Y-%m-%d')
        
        # 查询指定task_id今天的统计数据
        cursor.execute('''
        SELECT task_id, total_count, ng_count FROM count 
        WHERE task_id = ? AND production_date = ?
        ''', (task_id, today))
        task_result = cursor.fetchone()
        
        # 查询今天所有型号的汇总统计
        cursor.execute('''
        SELECT SUM(total_count) as total_all, SUM(ng_count) as ng_all 
        FROM count 
        WHERE production_date = ?
        ''', (today,))
        summary_result = cursor.fetchone()
        
        # 处理可能的None值
        if summary_result:
            current_status['total_counting'] = summary_result[0] or 0
            current_status['total_failed_counting'] = summary_result[1] or 0
        else:
            current_status['total_counting'] = 0
            current_status['total_failed_counting'] = 0
            
        if task_result:
            current_status['counting'] = task_result[1] or 0
            current_status['failed_counting'] = task_result[2] or 0
        else:
            current_status['counting'] = 0
            current_status['failed_counting'] = 0
        current_status['socketio'] = {'event': []}
        return current_status

def batch_write_to_db(db_name, data_list):
    """批量写入数据，提高性能"""
    if not data_list:
        return
        
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        
        # 准备批量插入的数据
        json_data_list = [(json.dumps(data),) for data in data_list]
        
        # 批量插入
        cursor.executemany(f'INSERT INTO {db_name} (data) VALUES (?)', json_data_list)
        conn.commit()


def cleanup_old_data(days_to_keep=7):
    """清理指定天数之前的数据"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        
        from datetime import datetime, timedelta
        cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            # 清理tasks表
            cursor.execute('DELETE FROM tasks WHERE created_at < ?', (cutoff_date,))
            tasks_deleted = cursor.rowcount
            
            # 清理ng表
            cursor.execute('DELETE FROM ng WHERE created_at < ?', (cutoff_date,))
            ng_deleted = cursor.rowcount
            
            conn.commit()
            
            g.logger.info(f"清理完成: tasks表删除{tasks_deleted}条，ng表删除{ng_deleted}条")
            return {'tasks_deleted': tasks_deleted, 'ng_deleted': ng_deleted}
            
        except Exception as e:
            g.logger.error(f"清理旧数据时发生错误: {e}")
            raise e

def write_current_task_id(task_id):
    """写入当前任务ID"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM current_task_id')
        cursor.execute('INSERT INTO current_task_id (current_task_id) VALUES (?)', (task_id,))
        conn.commit()

def read_current_task_id():
    """读取当前任务ID。如果表为空返回None，表不存在则抛出异常。"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT current_task_id FROM current_task_id')
        except sqlite3.OperationalError as e:
            # 表不存在时抛出异常
            if 'no such table' in str(e):
                raise
            else:
                raise
        row = cursor.fetchone()
        if row is not None:
            return row[0]
        else:
            return None

def write_tasks_content(tasks_content):
    """写入任务内容"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        try:
            # 将数据转换为JSON字符串
            if isinstance(tasks_content, dict):
                tasks_content_str = json.dumps(tasks_content)
            else:
                tasks_content_str = str(tasks_content)
                
            cursor.execute('DELETE FROM tasks_content')
            cursor.execute('INSERT INTO tasks_content (tasks_content) VALUES (?)', (tasks_content_str,))
            conn.commit()
        except Exception as e:
            g.logger.error(f"写入任务内容时发生错误: {e}")
            conn.rollback()
            raise e

def read_tasks_content():
    """读取任务内容"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT tasks_content FROM tasks_content ORDER BY ROWID DESC LIMIT 1')
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
            else:
                return None
        except Exception as e:
            g.logger.error(f"读取任务内容时发生错误: {e}")
            return None

def close_db_pool():
    """关闭数据库连接池"""
    global _db_pool
    if _db_pool:
        _db_pool.close_all()
        _db_pool = None
