#!/usr/bin/python
# -*- coding: utf-8 -*-

# @Time    : 2019-11-07
# @Author  : lework
# @Desc    : 针对supervisor的应用进行健康检查


import os
import sys
import time
import json
import yaml
import base64
import socket
import smtplib
import datetime
import platform
import threading
from xmlrpclib import ServerProxy, Fault
from email.header import Header
from email.mime.text import MIMEText
from collections import namedtuple

try:
    import httplib
except ImportError:
    import http.client as httplib


PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3

if PY3:
    def iterkeys(d, **kw):
        return iter(d.keys(**kw))


    def iteritems(d, **kw):
        return iter(d.items(**kw))
else:
    def iterkeys(d, **kw):
        return d.iterkeys(**kw)


    def iteritems(d, **kw):
        return d.iteritems(**kw)


def shell(cmd):
    """
    执行系统命令
    :param cmd:
    :return:
    """
    with os.popen(cmd) as f:
        return f.read()


def get_proc_cpu(pid):
    """
    获取进程CPU使用率
    :param pid:
    :return:
    """
    pscommand = 'ps -opcpu= -p %s'

    data = shell(pscommand % pid)
    if not data:
        # 未获取到数据值，或者没有此pid信息
        return None
    try:
        cpu_utilization = data.lstrip().rstrip()
        cpu_utilization = float(cpu_utilization)
    except ValueError:
        # 获取的结果不包含数据，或者无法识别cpu_utilization
        return None
    return cpu_utilization


def get_proc_rss(pid, cumulative=False):
    """
    获取进程内存使用
    :param pid:
    :param cumulative:
    :return:
    """
    pscommand = 'ps -orss= -p %s'
    pstreecommand = 'ps ax -o "pid= ppid= rss="'
    ProcInfo = namedtuple('ProcInfo', ['pid', 'ppid', 'rss'])

    def find_children(parent_pid, procs):
        # 找出进程的子进程信息
        children = []
        for proc in procs:
            pid, ppid, rss = proc
            if ppid == parent_pid:
                children.append(proc)
                children.extend(find_children(pid, procs))
        return children

    if cumulative:
        # 统计进程的子进程rss
        data = shell(pstreecommand)
        data = data.strip()

        procs = []
        for line in data.splitlines():
            pid, ppid, rss = map(int, line.split())
            procs.append(ProcInfo(pid=pid, ppid=ppid, rss=rss))

        # 计算rss
        try:
            parent_proc = [p for p in procs if p.pid == pid][0]
            children = find_children(pid, procs)
            tree = [parent_proc] + children
            rss = sum(map(int, [p.rss for p in tree]))
        except (ValueError, IndexError):
            # 计算错误时，返回None
            return None

    else:
        data = shell(pscommand % pid)
        if not data:
            # 未获取到数据值，或者没有此pid信息
            return None
        try:
            rss = data.lstrip().rstrip()
            rss = int(rss)
        except ValueError:
            # 获取的结果不包含数据，或者无法识别rss
            return None

    rss = rss / 1024  # rss 的单位是 KB， 这里返回MB单位
    return rss


class HealthCheck(object):
    def __init__(self, config):
        """
        初始化配置
        :param config:
        """

        self.mail_config = None
        self.wechat_config = None
        self.supervisor_url = 'http://localhost:9001/RPC2'

        if 'config' in config:
            self.mail_config = config['config'].get('mail')
            self.wechat_config = config['config'].get('wechat')
            self.supervisor_url = config['config'].get('supervistor_url', self.supervisor_url)
            config.pop('config')

        self.program_config = config

        self.periodSeconds = 5
        self.failureThreshold = 3
        self.successThreshold = 1
        self.initialDelaySeconds = 1

        self.max_rss = 1024
        self.cumulative = False
        self.max_cpu = 90

    def log(self, program, msg, *args):
        """
        写信息到 STDERR.
        :param str msg: string message.
        """

        curr_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        sys.stderr.write(
            '%s [%s] %s\n' % (curr_dt, program, msg % args,))

        sys.stderr.flush()

    def check(self, config):
        """
        检查主函数
        :param config:
        :return:
        """
        check_state = {}
        program = config.get('program')
        periodSeconds = config.get('periodSeconds', self.periodSeconds)
        failureThreshold = config.get('failureThreshold', self.failureThreshold)
        successThreshold = config.get('successThreshold', self.successThreshold)
        initialDelaySeconds = config.get('initialDelaySeconds', self.initialDelaySeconds)
        action_type = config.get('action', 'restart')

        check_type = config.get('type', 'HTTP').lower()
        check_method = self.http_check

        if check_type == 'tcp':
            check_method = self.tcp_check
        elif check_type == 'mem':
            check_method = self.mem_check
        elif check_type == 'cpu':
            check_method = self.cpu_check

        while True:
            if program not in check_state:
                check_state[program] = {
                    'periodSeconds': 1,
                    'failure': 0,
                    'success': 0,
                    'action': False
                }
                self.log(program, 'CONFIG: %s', config)
                time.sleep(initialDelaySeconds)

            # self.log(program, '%s check state: %s', check_type, json.dumps(check_state[program]))
            if check_state[program]['periodSeconds'] % periodSeconds == 0:
                check_result = check_method(config)
                check_status = check_result.get('status', 'unknow')
                check_info = check_result.get('info', '')
                self.log(program, '%s check: info(%s) state(%s)', check_type.upper(), check_info, check_status)

                if check_status == 'failure':
                    check_state[program]['failure'] += 1
                elif check_status == 'success':
                    check_state[program]['success'] += 1

                # 先判断成功次数
                if check_state[program]['success'] >= successThreshold:
                    # 成功后,将项目状态初始化
                    check_state[program]['failure'] = 0
                    check_state[program]['success'] = 0
                    check_state[program]['action'] = False

                # 再判断失败次数
                if check_state[program]['failure'] >= failureThreshold:
                    # 失败后, 只触发一次action，或者检测错误数是2倍periodSeconds的平方数时触发(避免重启失败导致服务一直不可用)
                    if not check_state[program]['action'] or (
                            check_state[program]['failure'] != 0 and check_state[program][
                        'failure'] % (periodSeconds * 2) == 0):
                        self.action(program, action_type, check_result.get('msg', ''))
                        check_state[program]['action'] = True

                # 间隔时间清0
                check_state[program]['periodSeconds'] = 0

            time.sleep(1)
            check_state[program]['periodSeconds'] += 1

    def http_check(self, config):
        """
        用于检查http连接
        :param config:
        :return: dict
        """
        program = config.get('program')
        config_host = config.get('host', 'localhost')
        config_path = config.get('path', '/')
        config_port = config.get('port', '80')

        config_method = config.get('method', 'GET')
        config_timeoutSeconds = config.get('timeoutSeconds', 3)
        config_body = config.get('body', '')
        config_json = config.get('json', '')
        config_hearders = config.get('hearders', '')

        config_username = config.get('username', '')
        config_password = config.get('password', '')

        HEADERS = {'User-Agent': 'leops http_check'}

        headers = HEADERS.copy()
        if config_hearders:
            try:
                headers.update(json.loads(config_hearders))
            except Exception as e:
                self.log(program, 'HTTP: config_headers not loads: %s , %s', config_hearders, e)
            if config_json:
                headers['Content-Type'] = 'application/json'

        if config_username and config_password:
            auth_str = '%s:%s' % (config_username, config_password)
            headers['Authorization'] = 'Basic %s' % base64.b64encode(auth_str.encode()).decode()

        if config_json:
            try:
                config_body = json.dumps(config_json)
            except Exception as e:
                self.log(program, 'HTTP: config_json not loads: %s , %s', json, e)

        check_info = '%s %s %s %s %s %s' % (config_host, config_port, config_path, config_method,
                 config_body, headers)

        try:
            httpClient = httplib.HTTPConnection(config_host, config_port, timeout=config_timeoutSeconds)
            httpClient.request(config_method, config_path, config_body, headers=headers)
            res = httpClient.getresponse()
        except Exception as e:
            self.log(program, 'HTTP: conn error, %s', e)
            return {'status': 'failure', 'msg': '[http_check] %s' % e, 'info': check_info}
        finally:
            if httpClient:
                httpClient.close()

        if res.status != httplib.OK:
            return {'status': 'failure', 'msg': '[http_check] return code %s' % res.status, 'info': check_info}
         
        return {'status': 'success','info': check_info}

    def tcp_check(self, config):
        """
        用于检查TCP连接
        :param config:
        :return: dict
        """
        program = config.get('program')
        host = config.get('host', 'localhost')
        port = config.get('port', 80)
        timeoutSeconds = config.get('timeoutSeconds', 3)
        check_info = '%s %s' % (host, port)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeoutSeconds)
            sock.connect((host, port))
            sock.close()
        except Exception as e:
            self.log(program, 'TCP: conn error, %s', e)
            return {'status': 'failure', 'msg': '[tcp_check] %s' % e, 'info': check_info}
        return {'status': 'success','info': check_info}

    def mem_check(self, config):
        """
        用于检查进程内存
        :param config:
        :return: dict
        """
        program = config.get('program')
        max_rss = config.get('max_rss', self.max_rss)
        cumulative = config.get('cumulative', self.cumulative)
        check_info = 'max_rss:%sMB cumulative:%s' % (max_rss,cumulative)

        try:
            s = ServerProxy(self.supervisor_url)
            info = s.supervisor.getProcessInfo(program)
            pid = info.get('pid')
            if pid == 0:
                self.log(program, 'MEM: check error, program not starting')
                return {'status': 'failure',
                        'msg': '[mem_check] program not starting, message: %s' % (info.get('description')),'info': check_info}
            now_rss = get_proc_rss(pid, cumulative)
            check_info = '%s now_rss:%sMB' % (check_info, now_rss)
            if now_rss >= int(max_rss):
                return {'status': 'failure', 'msg': '[mem_check] max_rss(%sMB) now_rss(%sMB)' % (max_rss, now_rss), 'info': check_info}
        except Exception as e:
            self.log(program, 'MEM: check error, %s', e)
            return {'status': 'failure', 'msg': '[mem_check] %s' % e,'info': check_info}

        return {'status': 'success','info': check_info}

    def cpu_check(self, config):
        """
        用于检查进程CPU
        :param config:
        :return: dict
        """
        program = config.get('program')
        max_cpu = config.get('max_cpu', self.max_cpu)
        check_info = 'max_cpu:{cpu}%'.format(cpu=max_cpu)

        try:
            s = ServerProxy(self.supervisor_url)
            info = s.supervisor.getProcessInfo(program)
            pid = info.get('pid')
            if pid == 0:
                self.log(program, 'CPU: check error, program not starting')
                return {'status': 'failure',
                        'msg': '[cpu_check] program not starting, message: %s' % (info.get('description')),'info': check_info}
            now_cpu = get_proc_cpu(pid)
            check_info = '{info} now_cpu:{now}%'.format(info=check_info, now=now_cpu)
            if now_cpu >= int(max_cpu):
                return {'status': 'failure', 'msg': '[cpu_check] max_cpu({max_cpu}%) now_cpu({now}%)'.format(max_cpu=max_cpu, now=now_cpu),'info': check_info}
        except Exception as e:
            self.log(program, 'CPU: check error, %s', e)
            return {'status': 'failure', 'msg': '[cpu_check] %s' % e,'info': check_info}

        return {'status': 'success','info': check_info}

    def action(self, program, action_type, error):
        """
        执行动作
        :param program:
        :param action_type:
        :param error:
        :return:
        """
        self.log(program, 'Action: %s', action_type)
        action_list = action_type.split(',')
        if 'restart' in action_list:
            restart_result = self.action_supervistor_restart(program)
            error += '\r\n Restart：%s' % restart_result
        if 'email' in action_list and self.mail_config:
            self.action_email(program, action_type, error)
        if 'wechat' in action_list and self.wechat_config:
            self.action_wechat(program, action_type, error)

    def action_supervistor_restart(self, program):
        """
        通过supervisor的rpc接口重启进程
        :param program:
        :return:
        """
        self.log(program, 'Action: restart')
        result = 'success'
        try:
            s = ServerProxy(self.supervisor_url)
            info = s.supervisor.getProcessInfo(program)
        except Exception as e:
            result = 'Get %s ProcessInfo Error: %s' % (program, e)
            self.log(program, 'Action: restart %s' % result)
            return result

        if info['state'] == 20:
            self.log(program, 'Action: restart stop process')
            try:
                stop_result = s.supervisor.stopProcess(program)
                self.log(program, 'Action: restart stop result %s', stop_result)
            except Fault as e:
                result = 'Failed to stop process %s, exiting: %s' % (program, e)
                self.log(program, 'Action: restart stop error %s', result)
                return result

            time.sleep(1)
            info = s.supervisor.getProcessInfo(program)

        if info['state'] != 20:
            self.log(program, 'Action: restart start process')
            try:
                start_result = s.supervisor.startProcess(program)
            except Fault as e:
                result = 'Failed to start process %s, exiting: %s' % (program, e)
                self.log(program, 'Action: restart start error %s', result)
                return result
            self.log(program, 'Action: restart start result %s', start_result)

        return result

    def action_email(self, program, action_type, msg):
        """
        发送email
        :param subject: str
        :param content: str
        :return: bool
        """
        self.log(program, 'Action: email')

        ip = ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        finally:
            s.close()

        hostname = platform.node().split('.')[0]
        system_platform = platform.platform()

        subject = "[Supervisor] %s health check Faild" % program
        curr_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        content = """
                DateTime: {curr_dt}
                Program: {program}
                IP: {ip}
                Hostname: {hostname}
                Platfrom: {system_platform}
                Action: {action}
                Msg: {msg}
        """.format(curr_dt=curr_dt, program=program, ip=ip, hostname=hostname, system_platform=system_platform,
                   action=action_type,
                   msg=msg)
        mail_port = self.mail_config.get('port', '')
        mail_host = self.mail_config.get('host', '')
        mail_user = self.mail_config.get('user', '')
        mail_pass = self.mail_config.get('pass', '')
        to_list = self.mail_config.get('to_list', [])

        msg = MIMEText(content, _subtype='plain', _charset='utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = mail_user
        msg['to'] = ",".join(to_list)
        try:
            s = smtplib.SMTP_SSL(mail_host, mail_port)
            s.login(mail_user, mail_pass)
            s.sendmail(mail_user, to_list, msg.as_string())
            s.quit()
        except Exception as e:
            self.log(program, 'Action: email send error %s' % e)
            return False

        self.log(program, 'Action: email send success.')
        return True

    def action_wechat(self, program, action_type, msg):
        """
        微信通知
        :param program:
        :param action_type:
        :param msg:
        :return:
        """
        self.log(program, 'Action: wechat')

        host = "qyapi.weixin.qq.com"

        corpid = self.wechat_config.get('corpid')
        secret = self.wechat_config.get('secret')
        agentid = self.wechat_config.get('agentid')
        touser = self.wechat_config.get('touser')
        toparty = self.wechat_config.get('toparty')
        totag = self.wechat_config.get('totag')

        headers = {
            'Content-Type': 'application/json'
        }

        access_token_url = '/cgi-bin/gettoken?corpid={id}&corpsecret={crt}'.format(id=corpid, crt=secret)
        try:
            httpClient = httplib.HTTPSConnection(host, timeout=10)
            httpClient.request("GET", access_token_url, headers=headers)
            response = httpClient.getresponse()
            token = json.loads(response.read())['access_token']
        except Exception as e:
            self.log(program, 'Action: wechat get token error %s' % e)
            return False
        finally:
            if httpClient:
                httpClient.close()

        send_url = '/cgi-bin/message/send?access_token={token}'.format(token=token)

        ip = ""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
        finally:
            s.close()

        hostname = platform.node().split('.')[0]
        system_platform = platform.platform()

        curr_dt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        title = "<font color=\"warning\">[Supervisor] %s health check Faild</font>" % program

        content = title \
                  + "\n> **详情信息**" \
                  + "\n> DataTime: " + curr_dt \
                  + "\n> Program: <font color=\"warning\">%s</font>" % program \
                  + "\n> IP: " + ip \
                  + "\n> Hostname: " + hostname \
                  + "\n> Platfrom: " + system_platform \
                  + "\n> Action: " + action_type \
                  + "\n> Msg: " + str(msg)

        data = {
            "msgtype": 'markdown',
            "agentid": agentid,
            "markdown": {'content': content},
            "safe": 0
        }

        if touser:
            data['touser'] = touser
        if toparty:
            data['toparty'] = toparty
        if toparty:
            data['totag'] = totag

        try:
            httpClient = httplib.HTTPSConnection(host, timeout=10)
            httpClient.request("POST", send_url, json.dumps(data), headers=headers)
            response = httpClient.getresponse()
            result = json.loads(response.read())
            if result['errcode'] != 0:
                self.log(program, 'Action: wechat send faild %s' % result)
                return False
        except Exception as e:
            self.log(program, 'Action: wechat send error %s' % e)
            return False
        finally:
            if httpClient:
                httpClient.close()

        self.log(program, 'Action: wechat send success')
        return True

    def start(self):
        """
        启动检测
        :return:
        """
        self.log('healthCheck:', 'start')
        threads = []

        for key, value in iteritems(self.program_config):
            item = value
            item['program'] = key
            t = threading.Thread(target=self.check, args=(item,))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join()


if __name__ == '__main__':
    # 获取当前目录下的配置文件,没有的话就生成个模板
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.yaml')
    if not os.path.exists(config_file):
        example_config = """
config:                                          # 脚本配置名称,请勿更改
  supervisord_url: http://localhost:9001/RPC2    # supervisor的rpc接口地址
#  mail:                                         # stmp配置
#    host: 'smtp.test.com'
#    port': '465'
#    user': 'ops@test.com'
#    pass': '123456'
#    to_list: ['test@test.com']
#  wechat:                                       # 企业微信通知配置
#    corpid: 
#    secret: 
#    agentid: 
#    touser: 
#    toparty: 
#    totag: 

cat1:                     # supervisor中配置的program名称
  type: mem               # 检查类型: http,tcp,mem,cpu  默认: http
  max_rss: 1024           # 单位MB, 默认: 1024
  cumulative: True        # 是否统计子进程的内存, 默认: False
  periodSeconds: 10       # 检查的频率(以秒为单位), 默认: 5
  initialDelaySeconds: 10 # 首次检查等待的时间(以秒为单位), 默认: 1
  failureThreshold: 3     # 检查成功后，最少连续检查失败多少次才被认定为失败, 默认: 3
  successThreshold: 2     # 失败后检查成功的最小连续成功次数, 默认：1
  action: restart,email   # 触发的动作: restart,email,wechat 默认: restart

cat2:                     # supervisor中配置的program名称
  type: cpu               # 检查类型: http,tcp,mem,cpu 默认: http
  max_cpu: 80             # cpu使用百分比,单位% 默认: 90%
  periodSeconds: 10       # 检查的频率(以秒为单位), 默认: 5
  initialDelaySeconds: 10 # 首次检查等待的时间(以秒为单位), 默认: 1
  failureThreshold: 3     # 检查成功后，最少连续检查失败多少次才被认定为失败, 默认: 3
  successThreshold: 2     # 失败后检查成功的最小连续成功次数, 默认：1
  action: restart,wechat  # 触发的动作: restart,email,wechat 默认: restart

cat3:
  type: HTTP
  mode: POST              # http动作：POST,GET 默认: GET
  host: 127.0.0.1         # 主机地址, 默认: localhost
  path: /                 # URI地址，默认: /
  port: 8080              # 检测端口，默认: 80
  json: '{"a":"b"}'       # POST的json数据
  hearders: '{"c":1}'     # http的hearder头部数据
  username: test          # 用于http的basic认证
  password: pass          # 用于http的basic认证
  periodSeconds: 10       # 检查的频率(以秒为单位), 默认: 5
  initialDelaySeconds: 10 # 首次检查等待的时间(以秒为单位), 默认: 1
  timeoutSeconds: 5       # 检查超时的秒数, 默认: 3
  failureThreshold: 3     # 检查成功后，最少连续检查失败多少次才被认定为失败, 默认: 3
  successThreshold: 2     # 失败后检查成功的最小连续成功次数, 默认：1
  action: restart,email   # 触发的动作: restart,email,wechat 默认: restart
cat4:
  type: TCP
  host: 127.0.0.1         # 主机地址, 默认: localhost
  port: 8082              # 检测端口，默认: 80
  periodSeconds: 10       # 检查的频率(以秒为单位), 默认: 5
  initialDelaySeconds: 10 # 首次检查等待的时间(以秒为单位), 默认: 1
  timeoutSeconds: 5       # 检查超时的秒数, 默认: 3
  failureThreshold: 3     # 检查成功后，最少连续检查失败多少次才被认定为失败, 默认: 3
  successThreshold: 2     # 失败后检查成功的最小连续成功次数, 默认：1
  action: restart,email   # 触发的动作: restart,email,wechat 默认: restart
"""
        with open(config_file, 'w') as f:
            f.write(example_config)

        print("\r\n\r\nThe configuration file has been initialized, please modify the file to start.")
        print("Config File: %s\r\n\r\n" % config_file)
        sys.exit(0)

    with open(config_file) as f:
        config = yaml.load(f)

    check = HealthCheck(config)
    check.start()
