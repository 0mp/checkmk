// +------------------------------------------------------------------+
// |             ____ _               _        __  __ _  __           |
// |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
// |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
// |           | |___| | | |  __/ (__|   <    | |  | | . \            |
// |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
// |                                                                  |
// | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
// +------------------------------------------------------------------+
//
// This file is part of Check_MK.
// The official homepage is at http://mathias-kettner.de/check_mk.
//
// check_mk is free software;  you can redistribute it and/or modify it
// under the  terms of the  GNU General Public License  as published by
// the Free Software Foundation in version 2.  check_mk is  distributed
// in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
// out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
// PARTICULAR PURPOSE. See the  GNU General Public License for more de-
// tails. You should have  received  a copy of the  GNU  General Public
// License along with GNU Make; see the file  COPYING.  If  not,  write
// to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
// Boston, MA 02110-1301 USA.

// Needed for strdup and S_ISSOCK
#define _XOPEN_SOURCE 500

#include "config.h"
#include <fcntl.h>
#include <pthread.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>  // IWYU pragma: keep
#include <sys/un.h>
#include <unistd.h>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <ostream>
#include <string>
#include <unordered_map>
#include <utility>
#include "ClientQueue.h"
#include "InputBuffer.h"
#include "Logger.h"
#include "OutputBuffer.h"
#include "Store.h"
#include "StringUtils.h"
#include "TimeperiodsCache.h"
#include "auth.h"
#include "data_encoding.h"
#include "global_counters.h"
#include "livestatus.h"
#include "nagios.h"
#include "strutil.h"
#include "waittriggers.h"

using mk::unsafe_tolower;
using std::string;
using std::unordered_map;

NEB_API_VERSION(CURRENT_NEB_API_VERSION)
#ifndef NAGIOS4
extern int event_broker_options;
#else
extern unsigned long event_broker_options;
#endif  // NAGIOS4
extern int enable_environment_macros;
extern char *log_file;

int g_idle_timeout_msec =
    300 * 1000; /* maximum idle time for connection in keep alive state */
int g_query_timeout_msec = 10 * 1000; /* maximum time for reading a query */

int g_num_clientthreads = 10; /* allow 10 concurrent connections per default */
int g_num_queued_connections =
    0; /* current number of queued connections (for statistics) */
int g_num_active_connections =
    0; /* current number of active connections (for statistics) */
size_t g_thread_stack_size = 65536; /* stack size of threads */
extern int g_disable_statehist_filtering;

#define false 0
#define true 1

void *g_nagios_handle;
int g_unix_socket = -1;
int g_max_fd_ever = 0;
char g_socket_path[4096];
char pnp_path_storage[4096];
char *g_pnp_path = pnp_path_storage;
char g_mk_inventory_path[4096];  // base path of Check_MK inventory files
char g_mk_logwatch_path[4096];   // base path of Check_MK logwatch files
static char fl_logfile_path[4096];
char g_mkeventd_socket_path[4096];
int g_debug_level = 0;
int g_should_terminate = false;
pthread_t g_mainthread_id;
pthread_t *g_clientthread_id;
unsigned long g_max_cached_messages = 500000;
unsigned long g_max_lines_per_logfile =
    1000000;  // do never read more than that number of lines from a logfile
unsigned long g_max_response_size = 100 * 1024 * 1024;  // limit answer to 10 MB
int g_thread_running = 0;
int g_thread_pid = 0;
int g_service_authorization = AUTH_LOOSE;
int g_group_authorization = AUTH_STRICT;
Encoding g_data_encoding = Encoding::utf8;

// Map to speed up access via name/alias/address
unordered_map<string, host *> fl_hosts_by_designation;

static Store *fl_store = nullptr;
static ClientQueue *fl_client_queue = nullptr;
TimeperiodsCache *g_timeperiods_cache = nullptr;

/* simple statistics data for TableStatus */
extern host *host_list;
extern service *service_list;
extern scheduled_downtime *scheduled_downtime_list;
extern int log_initial_states;

int g_num_hosts;
int g_num_services;

void count_hosts() {
    g_num_hosts = 0;
    for (host *h = host_list; h != nullptr; h = h->next) {
        g_num_hosts++;
    }
}

void count_services() {
    g_num_services = 0;
    for (service *s = service_list; s != nullptr; s = s->next) {
        g_num_services++;
    }
}

host *getHostByDesignation(const char *designation) {
    auto it = fl_hosts_by_designation.find(unsafe_tolower(designation));
    return it == fl_hosts_by_designation.end() ? nullptr : it->second;
}

void *voidp;

void livestatus_count_fork() { g_counters[COUNTER_FORKS]++; }

void livestatus_cleanup_after_fork() {
    // 4.2.2010: Deactivate the cleanup function. It might cause
    // more trouble than it tries to avoid. It might lead to a deadlock
    // with Nagios' fork()-mechanism...
    // store_deinit();
    struct stat st;

    int i;
    // We need to close our server and client sockets. Otherwise
    // our connections are inherited to host and service checks.
    // If we close our client connection in such a situation,
    // the connection will still be open since and the client will
    // hang while trying to read further data. And the CLOEXEC is
    // not atomic :-(

    // Eventuell sollte man hier anstelle von store_deinit() nicht
    // darauf verlassen, dass die ClientQueue alle Verbindungen zumacht.
    // Es sind ja auch Dateideskriptoren offen, die von Threads gehalten
    // werden und nicht mehr in der Queue sind. Und in store_deinit()
    // wird mit mutexes rumgemacht....
    for (i = 3; i < g_max_fd_ever; i++) {
        if (0 == fstat(i, &st) && S_ISSOCK(st.st_mode)) {
            close(i);
        }
    }
}

bool runningInLivestatusMainThread() {
    return g_mainthread_id == pthread_self();
}

void *main_thread(void *data __attribute__((__unused__))) {
    g_mainthread_id = pthread_self();
    g_thread_pid = getpid();
    while (g_should_terminate == 0) {
        do_statistics();
        if (g_thread_pid != getpid()) {
            Informational() << "I'm not the main process but " << getpid()
                            << "!";
            // return;
        }
        struct timeval tv;
        tv.tv_sec = 2;
        tv.tv_usec = 500 * 1000;

        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(g_unix_socket, &fds);
        int retval = select(g_unix_socket + 1, &fds, nullptr, nullptr, &tv);
        if (retval > 0 && FD_ISSET(g_unix_socket, &fds)) {
            int cc = accept(g_unix_socket, nullptr, nullptr);
            if (cc > g_max_fd_ever) {
                g_max_fd_ever = cc;
            }
            if (fcntl(cc, F_SETFD, FD_CLOEXEC) < 0) {
                Informational() << "Cannot set FD_CLOEXEC on client socket: "
                                << strerror(errno);
            }
            fl_client_queue->addConnection(cc);  // closes fd
            g_num_queued_connections++;
            g_counters[COUNTER_CONNECTIONS]++;
        }
    }
    Informational() << "Socket thread has terminated";
    return voidp;
}

void *client_thread(void *data __attribute__((__unused__))) {
    OutputBuffer output_buffer;
    while (g_should_terminate == 0) {
        int cc = fl_client_queue->popConnection();
        g_num_queued_connections--;
        g_num_active_connections++;
        if (cc >= 0) {
            if (g_debug_level >= 2) {
                Informational() << "Accepted client connection on fd " << cc;
            }
            InputBuffer input_buffer(cc, &g_should_terminate);
            bool keepalive = true;
            unsigned requestnr = 1;
            while (keepalive) {
                if (g_debug_level >= 2 && requestnr > 1) {
                    Informational() << "Handling request " << requestnr
                                    << " on same connection";
                }
                keepalive =
                    fl_store->answerRequest(&input_buffer, &output_buffer);
                output_buffer.flush(cc, &g_should_terminate);
                g_counters[COUNTER_REQUESTS]++;
                requestnr++;
            }
            close(cc);
        }
        g_num_active_connections--;
    }
    return voidp;
}

void start_threads() {
    count_hosts();
    count_services();

    if (g_thread_running == 0) {
        /* start thread that listens on socket */
        pthread_atfork(livestatus_count_fork, nullptr,
                       livestatus_cleanup_after_fork);
        pthread_create(&g_mainthread_id, nullptr, main_thread, nullptr);
        if (g_debug_level > 0) {
            Informational() << "Starting " << g_num_clientthreads
                            << " client threads";
        }

        int t;
        g_clientthread_id = reinterpret_cast<pthread_t *>(
            malloc(sizeof(pthread_t) * g_num_clientthreads));
        pthread_attr_t attr;
        pthread_attr_init(&attr);
        size_t defsize;
        if (g_debug_level >= 2 &&
            0 == pthread_attr_getstacksize(&attr, &defsize)) {
            Informational() << "Default stack size is " << defsize;
        }
        if (0 != pthread_attr_setstacksize(&attr, g_thread_stack_size)) {
            Informational() << "Error: Cannot set thread stack size to "
                            << g_thread_stack_size;
        } else {
            if (g_debug_level >= 2) {
                Informational() << "Setting thread stack size to "
                                << g_thread_stack_size;
            }
        }
        for (t = 0; t < g_num_clientthreads; t++) {
            pthread_create(&g_clientthread_id[t], &attr, client_thread,
                           nullptr);
        }

        g_thread_running = 1;
        pthread_attr_destroy(&attr);
    }
}

void terminate_threads() {
    if (g_thread_running != 0) {
        g_should_terminate = true;
        Informational() << "Waiting for main to terminate...";
        pthread_join(g_mainthread_id, nullptr);
        Informational() << "Waiting for client threads to terminate...";
        fl_client_queue->terminate();
        int t;
        for (t = 0; t < g_num_clientthreads; t++) {
            if (0 != pthread_join(g_clientthread_id[t], nullptr)) {
                Informational() << "Warning: could not join thread no. " << t;
            }
        }
        if (g_debug_level > 0) {
            Informational() << "Main thread + " << g_num_clientthreads
                            << " client threads have finished";
        }
        g_thread_running = 0;
        g_should_terminate = false;
    }
    free(g_clientthread_id);
}

int open_unix_socket() {
    struct stat st;
    if (0 == stat(g_socket_path, &st)) {
        if (0 == unlink(g_socket_path)) {
            Debug() << "Removed old left over socket file " << g_socket_path;
        } else {
            Alert() << "Cannot remove in the way file " << g_socket_path << ": "
                    << strerror(errno);
            return false;
        }
    }

    g_unix_socket = socket(PF_UNIX, SOCK_STREAM, 0);
    g_max_fd_ever = g_unix_socket;
    if (g_unix_socket < 0) {
        Critical() << "Unable to create UNIX socket: " << strerror(errno);
        return false;
    }

    // Imortant: close on exec -> check plugins must not inherit it!
    if (fcntl(g_unix_socket, F_SETFD, FD_CLOEXEC) < 0) {
        Informational() << "Cannot set FD_CLOEXEC on socket: "
                        << strerror(errno);
    }

    // Bind it to its address. This creates the file with the name g_socket_path
    struct sockaddr_un sockaddr;
    sockaddr.sun_family = AF_UNIX;
    strncpy(sockaddr.sun_path, g_socket_path, sizeof(sockaddr.sun_path));
    if (bind(g_unix_socket, reinterpret_cast<struct sockaddr *>(&sockaddr),
             sizeof(sockaddr)) < 0) {
        Error() << "Unable to bind adress " << g_socket_path
                << " to UNIX socket: " << strerror(errno);
        close(g_unix_socket);
        return false;
    }

    // Make writable group members (fchmod didn't do nothing for me. Don't know
    // why!)
    if (0 != chmod(g_socket_path, 0660)) {
        Error() << "Cannot chown unix socket at " << g_socket_path
                << " to 0660: " << strerror(errno);
        close(g_unix_socket);
        return false;
    }

    if (0 != listen(g_unix_socket, 3 /* backlog */)) {
        Error() << "Cannot listen to unix socket at " << g_socket_path << ": "
                << strerror(errno);
        close(g_unix_socket);
        return false;
    }

    if (g_debug_level > 0) {
        Informational() << "Opened UNIX socket " << g_socket_path;
    }
    return true;
}

void close_unix_socket() {
    unlink(g_socket_path);
    if (g_unix_socket >= 0) {
        close(g_unix_socket);
        g_unix_socket = -1;
    }
}

int broker_host(int event_type __attribute__((__unused__)),
                void *data __attribute__((__unused__))) {
    g_counters[COUNTER_NEB_CALLBACKS]++;
    return 0;
}

int broker_check(int event_type, void *data) {
    int result = NEB_OK;
    if (event_type == NEBCALLBACK_SERVICE_CHECK_DATA) {
        nebstruct_service_check_data *c =
            reinterpret_cast<nebstruct_service_check_data *>(data);
        if (c->type == NEBTYPE_SERVICECHECK_PROCESSED) {
            g_counters[COUNTER_SERVICE_CHECKS]++;
        }
    } else if (event_type == NEBCALLBACK_HOST_CHECK_DATA) {
        nebstruct_host_check_data *c =
            reinterpret_cast<nebstruct_host_check_data *>(data);
        if (c->type == NEBTYPE_HOSTCHECK_PROCESSED) {
            g_counters[COUNTER_HOST_CHECKS]++;
        }
    }
    trigger_notify_all(trigger_check());
    return result;
}

int broker_comment(int event_type __attribute__((__unused__)), void *data) {
    nebstruct_comment_data *co =
        reinterpret_cast<nebstruct_comment_data *>(data);
    fl_store->registerComment(co);
    g_counters[COUNTER_NEB_CALLBACKS]++;
    trigger_notify_all(trigger_comment());
    return 0;
}

int broker_downtime(int event_type __attribute__((__unused__)), void *data) {
    nebstruct_downtime_data *dt =
        reinterpret_cast<nebstruct_downtime_data *>(data);
    fl_store->registerDowntime(dt);
    g_counters[COUNTER_NEB_CALLBACKS]++;
    trigger_notify_all(trigger_downtime());
    return 0;
}

int broker_log(int event_type __attribute__((__unused__)),
               void *data __attribute__((__unused__))) {
    g_counters[COUNTER_NEB_CALLBACKS]++;
    g_counters[COUNTER_LOG_MESSAGES]++;
    trigger_notify_all(trigger_log());
    return 0;
}

int broker_command(int event_type __attribute__((__unused__)), void *data) {
    nebstruct_external_command_data *sc =
        reinterpret_cast<nebstruct_external_command_data *>(data);
    if (sc->type == NEBTYPE_EXTERNALCOMMAND_START) {
        g_counters[COUNTER_COMMANDS]++;
    }
    g_counters[COUNTER_NEB_CALLBACKS]++;
    trigger_notify_all(trigger_command());
    return 0;
}

int broker_state(int event_type __attribute__((__unused__)),
                 void *data __attribute__((__unused__))) {
    g_counters[COUNTER_NEB_CALLBACKS]++;
    trigger_notify_all(trigger_state());
    return 0;
}

int broker_program(int event_type __attribute__((__unused__)),
                   void *data __attribute__((__unused__))) {
    g_counters[COUNTER_NEB_CALLBACKS]++;
    trigger_notify_all(trigger_program());
    return 0;
}

const char *get_downtime_comment(char *host_name, char *svc_desc) {
    char *comment;
    int matches = 0;
    for (scheduled_downtime *dt_list = scheduled_downtime_list;
         dt_list != nullptr; dt_list = dt_list->next) {
        if (dt_list->type == HOST_DOWNTIME) {
            if (strcmp(dt_list->host_name, host_name) == 0) {
                matches++;
                comment = dt_list->comment;
            }
        }
        if (svc_desc != nullptr && dt_list->type == SERVICE_DOWNTIME) {
            if (strcmp(dt_list->host_name, host_name) == 0 &&
                strcmp(dt_list->service_description, svc_desc) == 0) {
                matches++;
                comment = dt_list->comment;
            }
        }
    }
    return matches == 0 ? "No comment"
                        : matches > 1 ? "Multiple Downtime Comments" : comment;
}

void livestatus_log_initial_states() {
    // Log DOWNTIME hosts
    for (host *h = host_list; h != nullptr; h = h->next) {
        if (h->scheduled_downtime_depth > 0) {
            char buffer[8192];
            snprintf(buffer, sizeof(buffer),
                     "HOST DOWNTIME ALERT: %s;STARTED;%s", h->name,
                     get_downtime_comment(h->name, nullptr));
            write_to_all_logs(buffer, NSLOG_INFO_MESSAGE);
        }
    }
    // Log DOWNTIME services
    for (service *s = service_list; s != nullptr; s = s->next) {
        if (s->scheduled_downtime_depth > 0) {
            char buffer[8192];
            snprintf(buffer, sizeof(buffer),
                     "SERVICE DOWNTIME ALERT: %s;%s;STARTED;%s", s->host_name,
                     s->description,
                     get_downtime_comment(s->host_name, s->description));
            write_to_all_logs(buffer, NSLOG_INFO_MESSAGE);
        }
    }
    // Log TIMERPERIODS
    g_timeperiods_cache->logCurrentTimeperiods();
}

int broker_event(int event_type __attribute__((__unused__)), void *data) {
    g_counters[COUNTER_NEB_CALLBACKS]++;
    struct nebstruct_timed_event_struct *ts =
        reinterpret_cast<struct nebstruct_timed_event_struct *>(data);
    if (ts->event_type == EVENT_LOG_ROTATION) {
        if (g_thread_running == 1) {
            livestatus_log_initial_states();
        } else if (log_initial_states == 1) {
            // initial info during startup
            // TODO(sp) The Nagios headers are (once again) not const-correct...
            write_to_all_logs(const_cast<char *>("logging initial states"),
                              NSLOG_INFO_MESSAGE);
        }
    }
    g_timeperiods_cache->update(ts->timestamp.tv_sec);
    return 0;
}

int broker_process(int event_type __attribute__((__unused__)), void *data) {
    struct nebstruct_process_struct *ps =
        reinterpret_cast<struct nebstruct_process_struct *>(data);
    switch (ps->type) {
        case NEBTYPE_PROCESS_START:
            for (host *hst = host_list; hst != nullptr; hst = hst->next) {
                if (const char *address = hst->address) {
                    fl_hosts_by_designation[unsafe_tolower(address)] = hst;
                }
                if (const char *alias = hst->alias) {
                    fl_hosts_by_designation[unsafe_tolower(alias)] = hst;
                }
                fl_hosts_by_designation[unsafe_tolower(hst->name)] = hst;
            }
            fl_store = new Store();
            fl_client_queue = new ClientQueue();
            g_timeperiods_cache = new TimeperiodsCache();
            break;
        case NEBTYPE_PROCESS_EVENTLOOPSTART:
            g_timeperiods_cache->update(time(nullptr));
            start_threads();
            break;
        default:
            break;
    }
    return 0;
}

int verify_event_broker_options() {
    int errors = 0;
    if ((event_broker_options & BROKER_PROGRAM_STATE) == 0) {
        Critical() << "need BROKER_PROGRAM_STATE (" << BROKER_PROGRAM_STATE
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_TIMED_EVENTS) == 0) {
        Critical() << "need BROKER_TIMED_EVENTS (" << BROKER_TIMED_EVENTS
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_SERVICE_CHECKS) == 0) {
        Critical() << "need BROKER_SERVICE_CHECKS (" << BROKER_SERVICE_CHECKS
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_HOST_CHECKS) == 0) {
        Critical() << "need BROKER_HOST_CHECKS (" << BROKER_HOST_CHECKS
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_LOGGED_DATA) == 0) {
        Critical() << "need BROKER_LOGGED_DATA (" << BROKER_LOGGED_DATA
                   << ") event_broker_option enabled to work.",
            errors++;
    }
    if ((event_broker_options & BROKER_COMMENT_DATA) == 0) {
        Critical() << "need BROKER_COMMENT_DATA (" << BROKER_COMMENT_DATA
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_DOWNTIME_DATA) == 0) {
        Critical() << "need BROKER_DOWNTIME_DATA (" << BROKER_DOWNTIME_DATA
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_STATUS_DATA) == 0) {
        Critical() << "need BROKER_STATUS_DATA (" << BROKER_STATUS_DATA
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_ADAPTIVE_DATA) == 0) {
        Critical() << "need BROKER_ADAPTIVE_DATA (" << BROKER_ADAPTIVE_DATA
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_EXTERNALCOMMAND_DATA) == 0) {
        Critical() << "need BROKER_EXTERNALCOMMAND_DATA ("
                   << BROKER_EXTERNALCOMMAND_DATA
                   << ") event_broker_option enabled to work.";
        errors++;
    }
    if ((event_broker_options & BROKER_STATECHANGE_DATA) == 0) {
        Critical() << "need BROKER_STATECHANGE_DATA ("
                   << BROKER_STATECHANGE_DATA
                   << ") event_broker_option enabled to work.";
        errors++;
    }

    return static_cast<int>(errors == 0);
}

void register_callbacks() {
    neb_register_callback(NEBCALLBACK_HOST_STATUS_DATA, g_nagios_handle, 0,
                          broker_host);  // Needed to start threads
    neb_register_callback(NEBCALLBACK_COMMENT_DATA, g_nagios_handle, 0,
                          broker_comment);  // dynamic data
    neb_register_callback(NEBCALLBACK_DOWNTIME_DATA, g_nagios_handle, 0,
                          broker_downtime);  // dynamic data
    neb_register_callback(NEBCALLBACK_SERVICE_CHECK_DATA, g_nagios_handle, 0,
                          broker_check);  // only for statistics
    neb_register_callback(NEBCALLBACK_HOST_CHECK_DATA, g_nagios_handle, 0,
                          broker_check);  // only for statistics
    neb_register_callback(NEBCALLBACK_LOG_DATA, g_nagios_handle, 0,
                          broker_log);  // only for trigger 'log'
    neb_register_callback(NEBCALLBACK_EXTERNAL_COMMAND_DATA, g_nagios_handle, 0,
                          broker_command);  // only for trigger 'command'
    neb_register_callback(NEBCALLBACK_STATE_CHANGE_DATA, g_nagios_handle, 0,
                          broker_state);  // only for trigger 'state'
    neb_register_callback(NEBCALLBACK_ADAPTIVE_PROGRAM_DATA, g_nagios_handle, 0,
                          broker_program);  // only for trigger 'program'
    neb_register_callback(NEBCALLBACK_PROCESS_DATA, g_nagios_handle, 0,
                          broker_process);  // used for starting threads
    neb_register_callback(NEBCALLBACK_TIMED_EVENT_DATA, g_nagios_handle, 0,
                          broker_event);  // used for timeperiods cache
}

void deregister_callbacks() {
    neb_deregister_callback(NEBCALLBACK_HOST_STATUS_DATA, broker_host);
    neb_deregister_callback(NEBCALLBACK_COMMENT_DATA, broker_comment);
    neb_deregister_callback(NEBCALLBACK_DOWNTIME_DATA, broker_downtime);
    neb_deregister_callback(NEBCALLBACK_SERVICE_CHECK_DATA, broker_check);
    neb_deregister_callback(NEBCALLBACK_HOST_CHECK_DATA, broker_check);
    neb_deregister_callback(NEBCALLBACK_LOG_DATA, broker_log);
    neb_deregister_callback(NEBCALLBACK_EXTERNAL_COMMAND_DATA, broker_command);
    neb_deregister_callback(NEBCALLBACK_STATE_CHANGE_DATA, broker_state);
    neb_deregister_callback(NEBCALLBACK_ADAPTIVE_PROGRAM_DATA, broker_program);
    neb_deregister_callback(NEBCALLBACK_PROCESS_DATA, broker_program);
    neb_deregister_callback(NEBCALLBACK_TIMED_EVENT_DATA, broker_event);
}

void check_path(const char *name, char *path) {
    struct stat st;
    if (0 == stat(path, &st)) {
        if (0 != access(path, R_OK)) {
            Error() << name << " '" << path
                    << "' not readable. Please fix permissions.";
            path[0] = 0;  // disable
        }
    } else {
        Error() << name << " '" << path << "' not existing!";
        path[0] = 0;  // disable
    }
}

void livestatus_parse_arguments(const char *args_orig) {
    /* set default socket path */
    strncpy(g_socket_path, DEFAULT_SOCKET_PATH, sizeof(g_socket_path));

    /* set default path to our logfile to be in the same path as nagios.log */
    strncpy(fl_logfile_path, log_file,
            sizeof(fl_logfile_path) - 16 /* len of "livestatus.log" */);
    char *slash = strrchr(fl_logfile_path, '/');
    if (slash == nullptr) {
        strncpy(fl_logfile_path, "/tmp/livestatus.log", 20);
    } else {
        strncpy(slash + 1, "livestatus.log", 15);
    }

    g_mkeventd_socket_path[0] = 0;

    /* there is no default PNP path */
    g_pnp_path[0] = 0;

    if (args_orig == nullptr) {
        return;  // no arguments, use default options
    }

    char *args = strdup(args_orig);
    while (char *token = next_field(&args)) {
        /* find = */
        char *part = token;
        char *left = next_token(&part, '=');
        char *right = next_token(&part, 0);
        if (right == nullptr) {
            strncpy(g_socket_path, left, sizeof(g_socket_path));
        } else {
            if (strcmp(left, "debug") == 0) {
                g_debug_level = atoi(right);
                Informational() << "Setting debug level to " << g_debug_level;
            } else if (strcmp(left, "log_file") == 0) {
                strncpy(fl_logfile_path, right, sizeof(fl_logfile_path));
            } else if (strcmp(left, "mkeventd_socket_path") == 0) {
                strncpy(g_mkeventd_socket_path, right,
                        sizeof(g_mkeventd_socket_path));
            } else if (strcmp(left, "max_cached_messages") == 0) {
                g_max_cached_messages = strtoul(right, nullptr, 10);
                Informational()
                    << "Setting max number of cached log messages to "
                    << g_max_cached_messages;
            } else if (strcmp(left, "max_lines_per_logfile") == 0) {
                g_max_lines_per_logfile = strtoul(right, nullptr, 10);
                Informational() << "Setting max number lines per logfile to "
                                << g_max_lines_per_logfile;
            } else if (strcmp(left, "thread_stack_size") == 0) {
                g_thread_stack_size = strtoul(right, nullptr, 10);
                Informational() << "Setting size of thread stacks to "
                                << g_thread_stack_size;
            } else if (strcmp(left, "max_response_size") == 0) {
                g_max_response_size = strtoul(right, nullptr, 10);
                Informational() << "Setting maximum response size to "
                                << g_max_response_size << " bytes ("
                                << (g_max_response_size / (1024.0 * 1024.0))
                                << " MB)";
            } else if (strcmp(left, "num_client_threads") == 0) {
                int c = atoi(right);
                if (c <= 0 || c > 1000) {
                    Informational()
                        << "Error: Cannot set num_client_threads to " << c
                        << ". Must be > 0 and <= 1000";
                } else {
                    Informational() << "Setting number of client threads to "
                                    << c;
                    g_num_clientthreads = c;
                }
            } else if (strcmp(left, "query_timeout") == 0) {
                int c = atoi(right);
                if (c < 0) {
                    Informational() << "Error: query_timeout must be >= 0";
                } else {
                    g_query_timeout_msec = c;
                    if (c == 0) {
                        Informational() << "Disabled query timeout!";
                    } else {
                        Informational()
                            << "Setting timeout for reading a query to " << c
                            << " ms";
                    }
                }
            } else if (strcmp(left, "idle_timeout") == 0) {
                int c = atoi(right);
                if (c < 0) {
                    Informational() << "Error: idle_timeout must be >= 0";
                } else {
                    g_idle_timeout_msec = c;
                    if (c == 0) {
                        Informational() << "Disabled idle timeout!";
                    } else {
                        Informational() << "Setting idle timeout to " << c
                                        << " ms";
                    }
                }
            } else if (strcmp(left, "service_authorization") == 0) {
                if (strcmp(right, "strict") == 0) {
                    g_service_authorization = AUTH_STRICT;
                } else if (strcmp(right, "loose") == 0) {
                    g_service_authorization = AUTH_LOOSE;
                } else {
                    Informational() << "Invalid service authorization mode. "
                                       "Allowed are strict and loose.";
                }
            } else if (strcmp(left, "group_authorization") == 0) {
                if (strcmp(right, "strict") == 0) {
                    g_group_authorization = AUTH_STRICT;
                } else if (strcmp(right, "loose") == 0) {
                    g_group_authorization = AUTH_LOOSE;
                } else {
                    Informational() << "Invalid group authorization mode. "
                                       "Allowed are strict and loose.";
                }
            } else if (strcmp(left, "pnp_path") == 0) {
                strncpy(g_pnp_path, right, sizeof(pnp_path_storage) - 1);
                // make sure, that trailing slash is always there
                if (right[strlen(right) - 1] != '/') {
                    strncat(g_pnp_path, "/",
                            sizeof(pnp_path_storage) - strlen(g_pnp_path) - 1);
                }
                check_path("PNP perfdata directory", g_pnp_path);
            } else if (strcmp(left, "mk_inventory_path") == 0) {
                strncpy(g_mk_inventory_path, right,
                        sizeof(g_mk_inventory_path) - 1);
                if (right[strlen(right) - 1] != '/') {
                    strncat(g_mk_inventory_path, "/",
                            sizeof(g_mk_inventory_path) -
                                strlen(g_mk_inventory_path) -
                                1);  // make sure, that trailing slash is there
                }
                check_path("Check_MK Inventory directory", g_mk_inventory_path);
            } else if (strcmp(left, "mk_logwatch_path") == 0) {
                strncpy(g_mk_logwatch_path, right,
                        sizeof(g_mk_logwatch_path) - 1);
                if (right[strlen(right) - 1] != '/') {
                    strncat(g_mk_logwatch_path, "/",
                            sizeof(g_mk_logwatch_path) -
                                strlen(g_mk_logwatch_path) -
                                1);  // make sure, that trailing slash is there
                }
                check_path("Check_MK logwatch directory", g_mk_logwatch_path);
            } else if (strcmp(left, "data_encoding") == 0) {
                if (strcmp(right, "utf8") == 0) {
                    g_data_encoding = Encoding::utf8;
                } else if (strcmp(right, "latin1") == 0) {
                    g_data_encoding = Encoding::latin1;
                } else if (strcmp(right, "mixed") == 0) {
                    g_data_encoding = Encoding::mixed;
                } else {
                    Informational() << "Invalid data_encoding " << right
                                    << ". Allowed are utf8, latin1 and mixed.";
                }
            } else if (strcmp(left, "livecheck") == 0) {
                Informational()
                    << "Livecheck has been removed from Livestatus. Sorry.";
            } else if (strcmp(left, "disable_statehist_filtering") == 0) {
                g_disable_statehist_filtering = atoi(right);
            } else {
                Informational() << "Ignoring invalid option " << left << "="
                                << right;
            }
        }
    }

    if (g_mkeventd_socket_path[0] == 0) {
        strncpy(g_mkeventd_socket_path, g_socket_path,
                sizeof(g_mkeventd_socket_path));
        char *slash = strrchr(g_mkeventd_socket_path, '/');
        char *pos = slash == nullptr ? g_mkeventd_socket_path : (slash + 1);
        strncpy(
            pos, "mkeventd/status",
            &g_mkeventd_socket_path[sizeof(g_mkeventd_socket_path)] - slash);
        g_mkeventd_socket_path[sizeof(g_mkeventd_socket_path) - 1] = 0;
    }
    Warning() << "g_socket_path=[" << g_socket_path
              << "], g_mkeventd_socket_path=[" << g_mkeventd_socket_path << "]";

    // free(args); won't free, since we use pointers?
}

void omd_advertize() {
    char *omd_site = getenv("OMD_SITE");
    if (omd_site != nullptr) {
        if (g_debug_level > 0) {
            Informational() << "Running on OMD site " << omd_site << ". Cool.";
        }
    } else {
        Informational()
            << "Hint: please try out OMD - the Open Monitoring Distribution";
        Informational() << "Please visit OMD at http://omdistro.org";
    }
}

// Called from Nagios after we have been loaded.
// cppcheck-suppress unusedFunction
extern "C" int nebmodule_init(int flags __attribute__((__unused__)), char *args,
                              void *handle) {
    g_nagios_handle = handle;
    livestatus_parse_arguments(args);
    open_logfile(fl_logfile_path);

    Informational() << "Livestatus " << VERSION
                    << " by Mathias Kettner. Socket: '" << g_socket_path << "'";
    Informational() << "Please visit us at http://mathias-kettner.de/";

    omd_advertize();

    if (open_unix_socket() == 0) {
        return 1;
    }

    if (verify_event_broker_options() == 0) {
        Critical() << "Fatal: bailing out. Please fix event_broker_options.";
        Critical() << "Hint: your event_broker_options are set to "
                   << event_broker_options << ". Try setting it to -1.";
        return 1;
    }
    if (g_debug_level > 0) {
        Informational()
            << "Your event_broker_options are sufficient for livestatus..";
    }

    if (enable_environment_macros == 1) {
        Informational() << "Warning: environment_macros are enabled. This "
                           "might decrease the overall nagios performance";
    }

    register_callbacks();

    /* Unfortunately, we cannot start our socket thread right now.
       Nagios demonizes *after* having loaded the NEB modules. When
       demonizing we are losing our thread. Therefore, we create the
       thread the first time one of our callbacks is called. Before
       that happens, we haven't got any data anyway... */

    Informational() << "Finished initialization. Further log messages go to "
                    << fl_logfile_path;
    return 0;
}

// Called from Nagios after before we are unloaded.
// cppcheck-suppress unusedFunction
extern "C" int nebmodule_deinit(int flags __attribute__((__unused__)),
                                int reason __attribute__((__unused__))) {
    Informational() << "deinitializing";
    terminate_threads();
    close_unix_socket();
    delete fl_store;
    fl_store = nullptr;
    delete fl_client_queue;
    fl_client_queue = nullptr;
    delete g_timeperiods_cache;
    g_timeperiods_cache = nullptr;
    deregister_callbacks();
    close_logfile();
    return 0;
}
