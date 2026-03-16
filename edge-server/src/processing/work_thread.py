import time
import src.core.config.global_var as g
import src.core.config.nacos_var as n
import threading
import os
import shutil
import src.core.auth.casdoor_request as casdoor_request
import time
import json
import subprocess
import requests
from pathlib import Path
from src.core.database.sqlite_exec import write_to_db, delete_min_id, read_min_id
from src.processing.model_processing import status_update_loop, LAST_ALARM_TIME
from src.utils.tool import throw_error

truncate_stream_video_set = set()

# 设备状态缓存
device_status_cache = {
    'stopped': None,
    'started': None
}

previous_disappear_line = 0

# ==================== 性能优化：事件驱动机制 ====================
class OptimizedEvents:
    """
    事件驱动优化：使用事件替代频繁轮询，减少CPU唤醒次数
    预计可降低 60-70% 的CPU占用
    """
    def __init__(self):
        # 消失线更新事件
        self.disappear_line_event = threading.Event()
        # 数据上报事件
        self.report_data_event = threading.Event()

# 全局事件实例
events = OptimizedEvents()

def trigger_disappear_line_update():
    """触发消失线更新事件（在修改 g.DISAPPEAR_LINE 后调用）"""
    events.disappear_line_event.set()

def trigger_report_data():
    """触发数据上报事件（在数据写入数据库后调用）"""
    events.report_data_event.set()
# ================================================================

def get_video_duration(video_path):
    """获取视频时长（秒）"""
    try:
        cmd = [
            '/usr/local/ffmpeg/bin/ffprobe', 
            '-v', 'error', 
            '-show_entries', 'format=duration', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            video_path
        ]
        output = subprocess.check_output(cmd).decode().strip()
        return float(output)
    except Exception as e:
        g.logger.error(f"获取视频时长失败: {e}")
        return None

def process_video_with_padding(video_path, video_type, segment_seconds):
    """
    处理视频：如果视频长度超过10分钟，截取尾部10分钟
    :param video_path: 视频文件路径（./rdk/upload/original 或 label 下）
    :param video_type: 'original' 或 'labeled'
    :param segment_seconds: 目标视频时长（秒）
    :return: 处理后的视频路径（工作目录下），如果失败返回 None
    """
    try:
        # 获取视频时长
        duration = get_video_duration(video_path)
        if duration is None:
            g.logger.error(f"无法获取视频时长: {video_path}")
            return None
        
        g.logger.info(f"处理视频 {video_path}，当前时长: {duration:.2f}秒")
        
        # 提取时间戳
        filename = os.path.basename(video_path)
        if video_type == 'original':
            prefix = "video_"
        else:
            prefix = "label_"
        
        try:
            current_timestamp = int(filename[len(prefix):-4])
        except ValueError:
            g.logger.error(f"无法解析时间戳: {filename}")
            return None
        
        # 创建工作目录
        work_dir = "./upload"
        work_subdir = os.path.join(work_dir, video_type)
        os.makedirs(work_subdir, exist_ok=True)
        
        processed_video = None
        final_start_timestamp = current_timestamp
        
        # 只有视频长度超过10分钟（600秒）才截取尾部10分钟
        if duration > 600:
            g.logger.info(f"视频时长 {duration:.2f}秒 > 10分钟，截取尾部10分钟")
            
            # 临时目录
            temp_dir = os.path.join(os.path.dirname(video_path), "temp_process")
            os.makedirs(temp_dir, exist_ok=True)
            
            try:
                # 创建临时文件
                temp_output = os.path.join(temp_dir, f"trimmed_{filename}")
                
                # 计算开始时间（从尾部往前10分钟）
                start_time = duration - 600
                
                cmd = [
                    '/usr/local/ffmpeg/bin/ffmpeg',
                    '-y',
                    '-ss', str(start_time),
                    '-i', video_path,
                    '-t', '600',
                    '-c', 'copy',
                    '-an',
                    temp_output,
                ]

                g.logger.info(f"执行截断: {' '.join(cmd)}")
                result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
                
                if result.stdout:
                    g.logger.debug(f"ffmpeg输出: {result.stdout}")
                if result.stderr:
                    g.logger.debug(f"ffmpeg信息: {result.stderr}")
                
                # 计算新的开始时间戳（当前时间戳 + 截断掉的时长）
                final_start_timestamp = int(current_timestamp + start_time)
                processed_video = temp_output
                
            finally:
                pass  # 临时目录会在最后清理
        else:
            g.logger.info(f"视频时长 {duration:.2f}秒 <= 10分钟，直接使用原视频")
            processed_video = video_path
        
        if processed_video is None:
            g.logger.error(f"处理视频失败")
            return None
        
        # 生成最终文件名（使用新的时间戳）
        final_filename = f"{prefix}{final_start_timestamp}.mp4"
        final_path = os.path.join(work_subdir, final_filename)
        
        # 移动到工作目录
        if processed_video == video_path:
            # 如果没有处理，直接复制
            shutil.copy2(video_path, final_path)
        else:
            # 移动处理后的文件
            shutil.move(processed_video, final_path)
            # 清理临时目录
            try:
                temp_dir = os.path.join(os.path.dirname(video_path), "temp_process")
                if os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
            except Exception as e:
                g.logger.warning(f"清理临时目录失败: {e}")
        
        g.logger.info(f"视频处理完成: {final_path}")
        
        return final_path
        
    except Exception as e:
        g.logger.error(f"处理视频失败: {e}")
        import traceback
        g.logger.error(traceback.format_exc())
        return None


def launch_thread():
    if not n.DEV_FLAG:
        threading.Thread(target=video_upload_loop).start()
            
    # # 无论是否为DEV_FLAG，只要有模型调用就需要启动状态更新线程
    if n.DEV_FLAG:
        threading.Thread(target=write_produce_data_loop).start()
        threading.Thread(target=status_update_loop).start()
        threading.Thread(target=report_produce_data_loop).start()
        threading.Thread(target=set_disappear_line_loop).start()

def video_upload_loop():
    """极简视频上传循环 - 最小CPU占用"""
    # 修改为工作目录下的 upload 路径
    original_dir = "./rdk/upload/original"
    label_dir = "./rdk/upload/label"
    global truncate_stream_video_set
    while True:
        time.sleep(5)
        if g.APP_STATUS == g.AppStatus.RUNNING:
            if not os.path.exists(original_dir):
                os.makedirs(original_dir, exist_ok=True)
                continue
            video_files = []
            video_extensions = [".mp4"]
            for filename in os.listdir(original_dir):
                if filename.startswith("video_") and any(filename.endswith(ext) for ext in video_extensions):
                    try:
                        # 提取时间戳
                        for ext in video_extensions:
                            if filename.endswith(ext):
                                timestamp_str = filename[6:-len(ext)]  # 去掉"video_"前缀和扩展名后缀
                                timestamp = int(timestamp_str)
                                video_files.append((timestamp, filename))
                                break
                    except ValueError:
                        continue  # 跳过无效的文件名

            # 如果没有视频文件，继续等待
            if not video_files:
                continue

            # 按时间戳排序，选择最早的文件
            video_files.sort(key=lambda x: x[0])
            
            # 跳过已在truncate_stream_video_set中的文件，获取下一个
            for earliest_timestamp, earliest_filename in video_files:
                if earliest_filename not in truncate_stream_video_set:
                    break
            else:
                # 如果所有文件都在set中，则直接结束
                continue
            original_file_path = os.path.join(original_dir, earliest_filename)
            label_file_path = None
            if n.DEV_FLAG:
                # 构造对应的 label 文件名：video_123.mp4 -> label_123.mp4
                label_filename = f"label_{earliest_timestamp}.mp4"
                label_file_path = os.path.join(label_dir, label_filename)
            files = {}
            file_handles = []  # 用于跟踪文件句柄，确保在适当时候关闭
            
            try:
                if original_file_path:
                    video_file = open(original_file_path, "rb")
                    file_handles.append(video_file)
                    files.update({os.path.basename(original_file_path): video_file})
            except Exception as e:
                g.logger.error(f"原始视频 {earliest_filename} 时出错: {e}")
                # 关闭已打开的文件句柄
                for handle in file_handles:
                    handle.close()
                continue
            try:
                if label_file_path:
                    video_file = open(label_file_path, "rb")
                    file_handles.append(video_file)
                    files.update({os.path.basename(label_file_path): video_file})
            except Exception as e:
                g.logger.error(f"标注视频 {earliest_filename} 时出错: {e}")
                # 关闭已打开的文件句柄
                for handle in file_handles:
                    handle.close()
                continue
            # 上传文件
            try:
                request_data = {
                    "frpc_port": str(g.FRP_PORT),
                    "operating_station": n.WORKSTATION_NAME,
                    "task_id": n.ISP_CLOUD_CONFIG['cur_task_id'],
                    "create_time": earliest_timestamp,
                    "finish_time": earliest_timestamp + n.VIDEO_CONFIG['segment_seconds'],
                    "product_info": {
                        "details": {}
                    }
                }
                if n.DEV_FLAG:
                    files_status = {
                        os.path.basename(original_file_path): 'success',
                        os.path.basename(label_file_path): 'label'
                    }
                    # 保留中文，避免默认 ensure_ascii=True 导致 \uXXXX 转义
                    request_data['files_status'] = json.dumps(files_status, ensure_ascii=False)
                else:
                    files_status = {
                        os.path.basename(original_file_path): 'record',
                    }
                    request_data['files_status'] = json.dumps(files_status, ensure_ascii=False)
                response = casdoor_request.post(
                    url=n.ISP_CLOUD_CONFIG['upload_record_cloud_path'],
                    data = request_data,
                    files = files,
                    timeout= 300  # 视频文件上传超时时间设置为600秒（10分钟）
                )

                if response.status_code == 200:
                    if original_file_path:
                        os.remove(original_file_path)
                    if label_file_path:
                        os.remove(label_file_path)
                    g.logger.info(f"视频上传成功，已删除文件: {earliest_filename}")
                else:
                    g.logger.error(f"视频上传失败: {response.status_code} {response.text}")

            except Exception as e:
                g.logger.info(f"上传视频 {earliest_filename} 时出错: {e}")
            finally:
                # 确保文件句柄被正确关闭
                for handle in file_handles:
                    try:
                        handle.close()
                    except:
                        pass

def write_produce_data_loop():
    from src.processing.model_processing import LAST_ALARM_TIME
    while True:
        try:
            if g.APP_STATUS == g.AppStatus.RUNNING:
                produce_data = g.PRODUCE_DATA_QUEUE.get()
                if produce_data['update_status_ret']['finish_result'] == False:
                    # 如果生产结果为不合格，则将不合格数据写入数据库
                    if n.TRICOLOUR_LIGHT_FLAG and g.TRICOLOUR_LIGHT_CLIENT is not None:
                        running_status = n.TRICOLOUR_LIGHT_STATUS.get('running_status', {})
                        g.TRICOLOUR_LIGHT_CLIENT.set_light(running_status.get('ng', 'red,buzzer'))
                        g.ALARM_STATUS = True
                        g.logger.info("===set ng===")
                        import src.processing.model_processing as mp
                        mp.LAST_ALARM_TIME = time.time()
                else:
                    if n.TRICOLOUR_LIGHT_FLAG and g.TRICOLOUR_LIGHT_CLIENT is not None and g.ALARM_STATUS == False:
                        running_status = n.TRICOLOUR_LIGHT_STATUS.get('running_status', {})
                        g.TRICOLOUR_LIGHT_CLIENT.set_light(running_status.get('ok', 'green'))
                        g.logger.info("===set ok===")
                        import src.processing.model_processing as mp
                        mp.LAST_ALARM_TIME = time.time()
                # 判断当前 SOP 的结果
                finish_result = produce_data['update_status_ret']['finish_result']
                is_success = finish_result is True or finish_result == True
                is_ng = not is_success
                
                # 判断是否需要处理视频：
                # 1. 如果是 NG，总是需要处理视频
                # 2. 如果是 success，只有当 UPLOAD_SUCCESS 为 True 时才处理视频
                need_process_video = is_ng or n.UPLOAD_SUCCESS
                
                g.logger.info(
                    f"SOP结果: {'SUCCESS' if is_success else 'NG'}, "
                    f"UPLOAD_SUCCESS={n.UPLOAD_SUCCESS}, "
                    f"需要处理视频={need_process_video}"
                )
                
                original_file = None
                labeled_file = None
                
                if need_process_video:
                    # 调用视频截断（注意方法名是 split_stream）
                    original_file, labeled_file = g.RDK_MANAGER.split_stream()
                    
                    # 获取文件名，如果为None则返回not_found
                    import os
                    original_file_name = os.path.basename(original_file) if original_file else "not_found"
                    labeled_file_name = os.path.basename(labeled_file) if labeled_file else "not_found"
                    truncate_stream_video_set.add(original_file_name)
                    truncate_stream_video_set.add(labeled_file_name)
                    
                    # 处理视频：检查长度，必要时截断或补齐，然后移动到工作目录
                    # rdk_manager 返回的是相对路径，需要加上 ./rdk/ 前缀
                    processed_original = None
                    processed_labeled = None
                    
                    if original_file:
                        original_file_path = f"./rdk/{original_file}"
                        # 处理原始视频（检查、截断/补齐、移动）
                        processed_original = process_video_with_padding(
                            original_file_path, 
                            'original', 
                            n.VIDEO_CONFIG['segment_seconds']
                        )
                        
                    if labeled_file:
                        labeled_file_path = f"./rdk/{labeled_file}"
                        # 处理标注视频（检查、截断/补齐、移动）
                        processed_labeled = process_video_with_padding(
                            labeled_file_path, 
                            'labeled', 
                            n.VIDEO_CONFIG['segment_seconds']
                        )
                    
                    # 更新文件路径为工作目录下的路径
                    original_file = processed_original if processed_original else "not_found"
                    labeled_file = processed_labeled if processed_labeled else "not_found"
                    
                    g.logger.info(f"视频处理完成: original={original_file}, labeled={labeled_file}")
                else:
                    g.logger.info("SUCCESS操作且UPLOAD_SUCCESS=False，跳过视频处理")
            
                # 构建任务数据
                task_data = {
                    "frpc_port": str(g.FRP_PORT),
                    "operating_station": n.WORKSTATION_NAME,
                    "task_id": produce_data['task_id'],
                    "create_time": produce_data['start_time'],
                    "finish_time": produce_data['end_time'],
                    "finish_result": produce_data['update_status_ret']['finish_result'],
                    "product_info": json.dumps({
                        'details': produce_data['update_status_ret']['details']
                    })
                }
                
                # 只有在需要处理视频时才添加 files_status 和 files_path
                if need_process_video:
                    task_data["files_status"] = json.dumps({
                        os.path.basename(original_file): 'success' if is_success else 'ng',
                        os.path.basename(labeled_file): 'label'
                    })
                    task_data["files_path"] = {
                        "original": original_file if original_file else "not_found",
                        "labeled": labeled_file if labeled_file else "not_found"
                    }
                else:
                    # 不需要处理视频时，设置为空
                    task_data["files_status"] = json.dumps({})
                    task_data["files_path"] = {}
                write_to_db("tasks", task_data)
                # 性能优化：数据写入后立即触发上报事件，无需等待轮询
                trigger_report_data()
                g.logger.info(f"write_produce_data_loop: {produce_data['update_status_ret']['finish_result']}")
            else:
                time.sleep(1)
        except Exception as e:
            g.logger.error(f"write_produce_data_loop 处理异常: {e}")
            # 继续循环，避免线程退出
            time.sleep(1)


def report_produce_data_loop():
    """
    优化版本：使用事件驱动 + 更长的超时
    - 当有数据时立即处理（通过事件触发）
    - 无数据时最多10秒检查一次（作为保底机制）
    """
    while True:
        try:
            # 等待事件触发或超时（10秒），相比原来的5秒进一步降低唤醒频率
            if events.report_data_event.wait(timeout=5):
                events.report_data_event.clear()
            
            task_data = read_min_id("tasks")
            
            if task_data:
                # 添加调试日志
                g.logger.info(f"读取到任务数据: task_id={task_data.get('task_id')}, finish_result={task_data.get('finish_result')}, type={type(task_data.get('finish_result'))}, UPLOAD_SUCCESS={n.UPLOAD_SUCCESS}")
                
                if task_data['create_time'] > task_data['finish_time']:
                    g.logger.error(f"任务 {task_data['task_id']} 创建时间大于结束时间，跳过")
                    delete_min_id("tasks")
                    continue
                
                files = {}
                file_handles = []  # 用于跟踪文件句柄
                file_paths = task_data.get('files_path', {})
                
                # 判断是否有视频需要处理
                has_video_files = file_paths and len(file_paths) > 0
                
                if has_video_files:
                    # 有视频文件，尝试打开并上传
                    try:
                        for file_path in file_paths.values():
                            if file_path == "not_found":
                                continue
                            if not os.path.exists(file_path):
                                g.logger.warning(f"文件不存在，跳过: {file_path}")
                                continue
                            video_file = open(file_path, "rb")
                            file_handles.append(video_file)
                            files.update({os.path.basename(file_path): video_file})
                    except Exception as e:
                        g.logger.error(f"打开文件时出错: {e}")
                        # 关闭已打开的文件句柄
                        for handle in file_handles:
                            handle.close()
                        # 删除失败的任务记录，避免死循环
                        g.logger.warning(f"删除失败的任务记录: {task_data.get('task_id', 'unknown')}")
                        delete_min_id("tasks")
                        continue
                else:
                    # 没有视频文件，删除 files_status 字段（如果存在）
                    g.logger.info("任务无需处理视频文件，仅上传数据")
                    if 'files_status' in task_data:
                        del task_data['files_status']
                    if 'files_path' in task_data:
                        del task_data['files_path']
                
                try:
                    # 设置上传限速：默认2MB/s (2 * 1024 * 1024 字节/秒)
                    # 可以根据实际带宽情况调整，例如：
                    # 1MB/s = 1024 * 1024
                    # 2MB/s = 2 * 1024 * 1024
                    # 5MB/s = 5 * 1024 * 1024
                    max_upload_speed = 1 * 1024 * 1024  # 2MB/s
                    
                    # 将 task_data 中的所有值转换为字符串，以便 MultipartEncoder 正确处理
                    upload_data = {}
                    for key, value in task_data.items():
                        if isinstance(value, (int, float)):
                            upload_data[key] = str(value)
                        elif isinstance(value, bool):
                            upload_data[key] = str(value)
                        elif isinstance(value, dict):
                            # 字典类型转换为 JSON 字符串
                            upload_data[key] = json.dumps(value, ensure_ascii=False)
                        elif isinstance(value, list):
                            # 列表类型转换为 JSON 字符串
                            upload_data[key] = json.dumps(value, ensure_ascii=False)
                        else:
                            # 字符串或其他类型直接使用
                            upload_data[key] = value
                    
                    response = casdoor_request.post(
                        url=n.ISP_CLOUD_CONFIG['upload_record_cloud_path'],
                        data=upload_data,
                        files=files,
                        timeout=300,  # 视频文件上传超时时间设置为300秒（5分钟）
                        max_upload_speed=max_upload_speed  # 限速上传，避免占用过多带宽
                    )
                    if response.status_code == 200:
                        # 只有在有视频文件时才删除文件
                        if has_video_files:
                            for file_path in file_paths.values():
                                if file_path == "not_found":
                                    continue
                                if os.path.exists(file_path):
                                    os.remove(file_path)
                                    g.logger.info(f"已删除上传成功的视频: {file_path}")
                        delete_min_id("tasks")
                        g.logger.info(f"任务 {task_data.get('task_id', 'unknown')} 处理成功")
                    else:
                        g.logger.error(f"上传失败: {response.status_code} {response.text}")
                        # 上传失败时也删除任务记录，避免死循环
                        delete_min_id("tasks")
                except Exception as e:
                    g.logger.error(f"上传任务时出错: {e}")
                    # 上传异常时也删除任务记录，避免死循环
                    delete_min_id("tasks")
                finally:
                    # 确保文件句柄被正确关闭
                    for handle in file_handles:
                        try:
                            handle.close()
                        except:
                            pass

            # 检查模型调用告警
            current_time = int(time.time())
            if g.ALARM_CONFIG["failed_flag"] == False and g.APP_STATUS == g.AppStatus.RUNNING:
                if current_time - g.ALARM_CONFIG["model_call_failed_timestamp"] > 5 * 60:
                    g.ALARM_CONFIG["failed_flag"] = True
                    throw_error("模型调用失败")
                    
        except Exception as e:
            g.logger.error(f"上报生产数据线程异常: {e}")
            time.sleep(10)


def set_disappear_line_loop():
    """
    优化版本：使用事件驱动替代固定间隔轮询
    - 当有更新时立即响应（通过事件触发）
    - 无更新时最多10秒检查一次（作为保底机制）
    """
    while True:
        global previous_disappear_line
        try:
            # 等待事件触发，最多等待10秒（超时作为保底机制）
            if events.disappear_line_event.wait(timeout=20):
                events.disappear_line_event.clear()
            
            if g.DISAPPEAR_LINE is not None and previous_disappear_line != g.DISAPPEAR_LINE:
                if g.RDK_MANAGER.set_disappear_line(g.DISAPPEAR_LINE):
                    previous_disappear_line = g.DISAPPEAR_LINE
        except Exception as e:
            g.logger.error(f"设置检测线失败: {e}")
            time.sleep(10)
