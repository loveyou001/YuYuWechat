import json
import os
import subprocess
from datetime import datetime
from functools import wraps

import requests
from croniter import croniter
from django.conf import settings
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMessage, get_connection
from django.core.management import call_command
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import EmailSettings
from .models import Message, WechatUser, ServerConfig, ScheduledMessage, Log, ErrorLog, MessageCheck


def login_view(request):
    if request.method == 'POST':
        username = request.POST['username']
        password = request.POST['password']
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect('home')  # 登录成功后重定向到首页
        else:
            return render(request, 'login.html', {'error': 'Invalid username or password'})

    return render(request, 'login.html')


def log_activity(func):
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        response = None
        result = True
        return_data = ""

        # 尝试调用函数并捕获返回数据
        try:
            response = func(request, *args, **kwargs)
            if isinstance(response, JsonResponse):
                return_data = response.content.decode('utf-8')

        except Exception as e:
            result = False
            return_data = str(e)
            response = JsonResponse({'status': 'error', 'message': str(e)}, status=500)

        # 获取函数名称
        function_name = func.__name__

        # 记录日志
        Log.objects.create(
            result=result,
            function_name=function_name,
            return_data=return_data
        )

        return response

    return wrapper


@log_activity
def get_server_ip(request):
    server_ip = ServerConfig.objects.latest('id').server_ip if ServerConfig.objects.exists() else "none"
    return JsonResponse({'server_ip': server_ip})


@csrf_exempt
@log_activity
def set_server_ip(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        server_ip = data.get('server_ip')
        if server_ip:
            # 删除现有的所有IP记录
            ServerConfig.objects.all().delete()
            # 添加新的IP记录
            ServerConfig.objects.create(server_ip=server_ip)
            return JsonResponse({'status': f"Server IP set to {server_ip}"})
        else:
            return JsonResponse({'status': "No IP address provided"}, status=400)
    return JsonResponse({'status': "Invalid request method"}, status=405)


@login_required
@log_activity
def home(request):
    messages = Message.objects.all()
    groups = WechatUser.objects.values_list('group', flat=True).distinct()  # 获取所有分组
    return render(request, 'home.html', {'messages': messages, 'groups': groups})


@login_required
@log_activity
def send_message_management(request):
    messages = Message.objects.all()
    groups = WechatUser.objects.values_list('group', flat=True).distinct().order_by('group')
    return render(request, 'send_message_management.html', {'messages': messages, 'groups': groups})


@log_activity
@login_required
def schedule_management(request):
    tasks = ScheduledMessage.objects.all()
    now = timezone.localtime(timezone.now())

    # 检查 Celery 是否运行
    celery_running = False
    try:
        result = subprocess.run(['pgrep', '-f', 'celery'], stdout=subprocess.PIPE)
        celery_running = bool(result.stdout)
    except Exception as e:
        pass

    if not celery_running:
        celery_status = "celery未运行"
    else:
        celery_status = ""

    for task in tasks:
        if not celery_running:
            task.next_run = celery_status
        elif task.is_active and task.execution_count > 0:
            # 计算下次执行时间
            base = now
            iter = croniter(task.cron_expression, base)
            next_time = iter.get_next(datetime)
            skip_count = task.execution_skip

            # 跳过指定次数的执行时间
            while skip_count > 0:
                next_time = iter.get_next(datetime)
                skip_count -= 1

            task.next_run = next_time
        else:
            task.next_run = "不运行"

    # 获取所有分组，并按字典顺序排序
    groups = WechatUser.objects.values_list('group', flat=True).distinct().order_by('group')

    return render(request, 'schedule_management.html',
                  {'tasks': tasks, 'groups': groups, 'celery_status': celery_status})


@login_required
def message_check_view(request):
    tasks = MessageCheck.objects.all()
    now = timezone.localtime(timezone.now())

    for task in tasks:
        if task.is_active:
            # 计算下次执行时间
            base = now
            iter = croniter(task.cron_expression, base)
            next_time = iter.get_next(datetime)
            task.next_run = next_time
        else:
            task.next_run = "不运行"

    # 获取所有分组，并按字典顺序排序
    groups = WechatUser.objects.values_list('group', flat=True).distinct().order_by('group')

    return render(request, 'message_check.html',
                  {'tasks': tasks, 'groups': groups})


@csrf_exempt
@log_activity
def skip_execution(request):
    # 这里是提前发送的处理函数
    if request.method == 'POST':
        task_id = request.POST.get('task_id')
        try:
            task = ScheduledMessage.objects.get(id=task_id)
            task.execution_skip += 1
            task.save()

            # 发送消息
            user = task.user
            server_ip = ServerConfig.objects.latest('id').server_ip

            if not server_ip:
                return JsonResponse({'status': "Server IP not set"}, status=400)

            data = {
                'name': user.username,
                'text': task.text
            }

            url = f'http://{server_ip}/wechat/send_message/'
            response = requests.post(
                url,
                headers={'Content-Type': 'application/json'},
                data=json.dumps(data)
            )

            if response.ok:
                return JsonResponse({'status': f"Message sent to {user.username}"})
            else:
                return JsonResponse({'status': "Failed to send message"}, status=500)

        except ScheduledMessage.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '任务不存在'}, status=404)
    return JsonResponse({'status': 'error', 'message': '无效请求'}, status=400)


@csrf_exempt
@log_activity
def send_message(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        text = request.POST.get('text')
        server_ip = ServerConfig.objects.latest('id').server_ip  # 更新获取IP的方式

        if not server_ip:
            return JsonResponse({'status': "Server IP not set"}, status=400)

        try:
            user = WechatUser.objects.get(username=username)
        except WechatUser.DoesNotExist:
            return JsonResponse({'status': f"User {username} does not exist"}, status=400)

        data = {
            'name': username,
            'text': text
        }

        url = f'http://{server_ip}/wechat/send_message/'
        try:
            response = requests.post(
                url,
                headers={'Content-Type': 'application/json'},
                data=json.dumps(data),
                timeout=20  # 设置超时时间为20秒
            )

            if response.status_code == 200:
                return JsonResponse({'status': f"{text} sent to {username}"}, status=200)
            else:
                # 当服务器返回非200状态码时，记录错误日志
                ErrorLog.objects.create(
                    error_type="发送消息失败",
                    error_detail=f"给{username}发送{text}失败",
                    task_id="N/A"  # 如果有任务ID可替换此处
                )
                return JsonResponse({'status': f"Failed to send {text} to {username}"}, status=500)

        except requests.exceptions.RequestException as e:
            # 捕获请求异常并记录错误日志
            ErrorLog.objects.create(
                error_type="发送消息失败",
                error_detail=f"给{username}发送{text}失败，错误信息: {str(e)}",
                task_id="N/A"
            )
            return JsonResponse({'status': "Failed to send message due to a network error"}, status=500)

    return JsonResponse({'status': "Invalid request method"}, status=405)


@log_activity
@csrf_exempt
def export_database(request):
    if request.method == 'POST':
        file_path = os.path.join(settings.BASE_DIR, 'db_backup.json')
        with open(file_path, 'w') as f:
            call_command('dumpdata', 'client_app', stdout=f)  # 只导出 client_app 应用的数据
        with open(file_path, 'rb') as f:
            response = HttpResponse(f.read(), content_type='application/json')
            response['Content-Disposition'] = 'attachment; filename=db_backup.json'
            return response
    return render(request, 'export.html')


@csrf_exempt
@log_activity
def import_database(request):
    if request.method == 'POST':
        file = request.FILES['db_file']
        file_path = os.path.join(settings.BASE_DIR, 'temp_db.json')
        with open(file_path, 'wb+') as destination:
            for chunk in file.chunks():
                destination.write(chunk)

        # 删除现有的所有ServerConfig记录
        ServerConfig.objects.all().delete()

        try:
            call_command('loaddata', file_path)
            os.remove(file_path)
            return HttpResponse('Database imported successfully.')
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    return render(request, 'import.html')


@csrf_exempt
@log_activity
def start_celery(request):
    try:
        subprocess.Popen(['celery', '-A', 'YuYuWechatV2_Client', 'worker', '--loglevel=info'])
        subprocess.Popen(['celery', '-A', 'YuYuWechatV2_Client', 'beat', '--loglevel=info'])
        return JsonResponse({'status': 'Celery started'}, status=200)
    except Exception as e:
        return JsonResponse({'status': 'Failed to start Celery', 'error': str(e)}, status=500)


@log_activity
def stop_celery(request):
    try:
        subprocess.call(['pkill', '-f', 'celery'])
        return JsonResponse({'status': 'Celery stopped'}, status=200)
    except Exception as e:
        return JsonResponse({'status': 'Failed to stop Celery', 'error': str(e)}, status=500)


@log_activity
def check_celery_running(request):
    try:
        # 检查系统中运行的进程并搜索包含'celery'的进程
        result = subprocess.run(['pgrep', '-f', 'celery'], stdout=subprocess.PIPE)
        if result.stdout:
            return JsonResponse({'status': 'Celery is running'}, status=200)
        else:
            return JsonResponse({'status': 'Celery is not running'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'Failed to check Celery status', 'error': str(e)}, status=500)


@csrf_exempt
@log_activity
def check_wechat_status(request):
    try:
        # 从数据库中提取最新的服务器IP
        server_ip = ServerConfig.objects.latest('id').server_ip
        url = f"http://{server_ip}/wechat/check_wechat_status/"

        # 发送POST请求测试服务器链接
        response = requests.post(url, timeout=3)

        if response.status_code == 200:
            return JsonResponse({'status': 'success', 'message': 'WeChat status checked successfully'})
        else:
            return JsonResponse({'status': 'failure', 'message': '微信不在线'})
    except ServerConfig.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'No server IP configured'})
    except requests.exceptions.Timeout:
        return JsonResponse({'status': 'error', 'message': '未连接到服务器'})
    except requests.exceptions.RequestException as e:
        return JsonResponse({'status': 'error', 'message': str(e)})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})


@csrf_exempt
@log_activity
def ping_server(request):
    error_type = "无法连接到服务器"

    try:
        data = json.loads(request.body)  # 获取前端发送的 JSON 数据
        server_ip = data.get('server_ip', None)

        if not server_ip:
            error_detail = "没有设置服务器IP"
            if not ErrorLog.objects.filter(error_type=error_type).exists():
                ErrorLog.objects.create(error_type=error_type, error_detail=error_detail)
            return JsonResponse({'status': 'error', 'message': error_detail}, status=400)
    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': '无效的请求数据'}, status=400)

    try:
        url = f'http://{server_ip}/wechat/ping/'
        response = requests.get(url, timeout=3)  # 设置超时时间为3秒
        if response.status_code != 200:
            raise requests.RequestException(f"Ping failed with status code {response.status_code}")

        # 没有错误，删除现有的相关错误记录
        ErrorLog.objects.filter(error_type=error_type).delete()
        return JsonResponse({'status': 'success', 'message': '已连接到服务器'}, status=200)

    except requests.Timeout:
        error_detail = "ping超时"
        if not ErrorLog.objects.filter(error_type=error_type).exists():
            ErrorLog.objects.create(error_type=error_type, error_detail=error_detail)
        return JsonResponse({'status': 'error', 'message': error_detail}, status=500)

    except requests.RequestException as e:
        error_detail = f"ping服务器失败: {e}"
        if not ErrorLog.objects.filter(error_type=error_type).exists():
            ErrorLog.objects.create(error_type=error_type, error_detail=error_detail)
        return JsonResponse({'status': 'error', 'message': error_detail}, status=500)


@login_required
def log_view(request):
    filter_type = request.GET.get('filter', 'all')

    if filter_type == 'success':
        log_list = Log.objects.filter(result=True).order_by('-timestamp')
    elif filter_type == 'failure':
        log_list = Log.objects.filter(result=False).order_by('-timestamp')
    else:
        log_list = Log.objects.all().order_by('-timestamp')

    paginator = Paginator(log_list, 100)  # 每页显示100条记录
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'log.html', {'page_obj': page_obj, 'filter': filter_type})


def log_counts(request):
    total_logs = Log.objects.count()
    success_logs = Log.objects.filter(result=True).count()
    failure_logs = Log.objects.filter(result=False).count()
    return JsonResponse({
        'total': total_logs,
        'success': success_logs,
        'failure': failure_logs,
    })


@csrf_exempt
def clear_logs(request):
    if request.method == 'POST':
        Log.objects.all().delete()
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'invalid method'}, status=405)


def check_scheduled_message_errors():
    errors = []
    now = timezone.localtime(timezone.now())

    tasks = ScheduledMessage.objects.all()
    for task in tasks:
        if task.is_active:
            iter = croniter(task.cron_expression, now)
            last_execution_time = iter.get_prev(datetime)

            if task.last_executed is None or task.last_executed < last_execution_time:
                errors.append({
                    'error_type': '定时任务遗漏',
                    'error_detail': (
                        f"应该在 <span class='highlight'>{last_execution_time.strftime('%Y-%m-%d %H:%M:%S')}</span> "
                        f"给 <span class='highlight'>{task.user.username}</span> 发送 "
                        f"<span class='highlight'>{task.text}</span> 未能发送"
                    ),
                    'task_id': task.id,
                    'correct_time': last_execution_time.strftime('%Y-%m-%d %H:%M:%S')
                })

    return errors


@login_required
@log_activity
def error_detection_view(request):
    errors = ErrorLog.objects.all().order_by('-timestamp')
    return render(request, 'error_detection.html', {'errors': errors})


def check_errors(request):
    # 统计数据库中的错误数量
    error_count = ErrorLog.objects.count()
    return JsonResponse({'errors': error_count})


@csrf_exempt
@log_activity
def handle_error_cron(request):
    # 这里是处理定时任务遗漏的函数
    if request.method == 'POST':
        data = json.loads(request.body)
        action = data.get('action')
        error_id = data.get('task_id')
        correct_time_str = data.get('correct_time')

        try:
            error_log = ErrorLog.objects.get(id=int(error_id))
            task_id = error_log.task_id
            task = ScheduledMessage.objects.get(id=int(task_id))

            if correct_time_str:
                correct_time = datetime.strptime(correct_time_str, '%Y-%m-%d %H:%M:%S')
            else:
                correct_time = timezone.now()  # 如果没有提供时间，则使用当前时间

            if action == 'ignore':
                task.last_executed = correct_time
                task.save()
                # 删除错误日志
                error_log.delete()
                return JsonResponse({'status': 'success', 'message': '错误已忽略并删除'})
            elif action == 'resend':
                user = task.user
                server_ip = ServerConfig.objects.latest('id').server_ip

                if not server_ip:
                    return JsonResponse({'status': "Server IP not set"}, status=400)

                data = {
                    'name': user.username,
                    'text': task.text
                }

                url = f'http://{server_ip}/wechat/send_message/'
                response = requests.post(
                    url,
                    headers={'Content-Type': 'application/json'},
                    data=json.dumps(data)
                )

                if response.ok:
                    task.last_executed = correct_time
                    task.save()
                    # 删除错误日志
                    error_log.delete()
                    return JsonResponse({'status': 'success', 'message': '消息已补发并修正错误，日志已删除'})
                else:
                    return JsonResponse({'status': 'error', 'message': '消息补发失败'}, status=500)
        except ScheduledMessage.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '任务不存在'}, status=404)
        except ErrorLog.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '错误日志不存在'}, status=404)
        except ValueError:
            return JsonResponse({'status': 'error', 'message': '时间格式错误'}, status=400)

    return JsonResponse({'status': 'invalid method'}, status=405)


@csrf_exempt
@log_activity
def delete_chat_record_error(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        error_id = data.get('task_id')

        try:
            # 查找对应的错误日志
            error_log = ErrorLog.objects.get(id=int(error_id))

            # 确认错误类型是聊天记录检测错误
            if error_log.error_type == '聊天记录检测错误':
                # 删除错误日志
                error_log.delete()
                return JsonResponse({'status': 'success', 'message': '聊天记录检测错误已删除'})
            else:
                return JsonResponse({'status': 'error', 'message': '错误类型不匹配'}, status=400)

        except ErrorLog.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '错误日志不存在'}, status=404)
        except ValueError:
            return JsonResponse({'status': 'error', 'message': '无效的错误ID'}, status=400)

    return JsonResponse({'status': 'invalid method'}, status=405)

def send_email(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            subject = data.get('subject', 'YuYuWechat测试邮件')
            message = data.get('message', '邮件自动报警功能正常')

            email_settings = EmailSettings.objects.first()
            if email_settings:
                connection_kwargs = {
                    'backend': 'django.core.mail.backends.smtp.EmailBackend',
                    'host': email_settings.email_host,
                    'port': email_settings.email_port,
                    'username': email_settings.email_host_user,
                    'password': email_settings.email_host_password,
                }

                if email_settings.email_security == 'tls':
                    connection_kwargs['use_tls'] = True
                    connection_kwargs['use_ssl'] = False
                else:
                    connection_kwargs['use_tls'] = False
                    connection_kwargs['use_ssl'] = True

                connection = get_connection(**connection_kwargs)

                email = EmailMessage(
                    subject,
                    message,
                    email_settings.default_from_email,
                    email_settings.recipient_list.split(','),
                    connection=connection,
                )
                email.send()
                return JsonResponse({"status": "success", "message": "Email sent successfully."})
            else:
                return JsonResponse({"status": "error", "message": "Email settings are not configured."}, status=400)
        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
    else:
        return JsonResponse({"status": "error", "message": "Invalid request method."}, status=405)


@csrf_exempt
@log_activity
def check_email_settings(request):
    # 检查 Celery 是否运行
    result = subprocess.run(['pgrep', '-f', 'celery'], stdout=subprocess.PIPE)
    celery_running = bool(result.stdout)

    # 检查邮箱配置是否存在
    email_settings = EmailSettings.objects.exists()

    if celery_running and email_settings:
        return JsonResponse({'status': 'ok', 'message': '邮箱配置正确且Celery运行中'})
    elif not celery_running:
        return JsonResponse({'status': 'error', 'message': 'Celery未运行'})
    else:
        return JsonResponse({'status': 'error', 'message': '邮箱未配置'})
