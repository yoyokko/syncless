/* Header extracted from libev-3.9/event.h
 * by pts@fazekas.hu at Sat Apr 24 22:27:08 CEST 2010
 */

/*
 * libevent compatibility header, only core events supported
 *
 * Copyright (c) 2007,2008 Marc Alexander Lehmann <libev@schmorp.de>
 * All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without modifica-
 * tion, are permitted provided that the following conditions are met:
 * 
 *   1.  Redistributions of source code must retain the above copyright notice,
 *       this list of conditions and the following disclaimer.
 * 
 *   2.  Redistributions in binary form must reproduce the above copyright
 *       notice, this list of conditions and the following disclaimer in the
 *       documentation and/or other materials provided with the distribution.
 * 
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR IMPLIED
 * WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MER-
 * CHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.  IN NO
 * EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPE-
 * CIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 * PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
 * OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
 * WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTH-
 * ERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
 * OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 * Alternatively, the contents of this file may be used under the terms of
 * the GNU General Public License ("GPL") version 2 or any later version,
 * in which case the provisions of the GPL are applicable instead of
 * the above. If you wish to allow the use of your version of this file
 * only under the terms of the GPL and not to allow others to use your
 * version of this file under the BSD license, indicate your decision
 * by deleting the provisions above and replace them with the notice
 * and other provisions required by the GPL. If you do not delete the
 * provisions above, a recipient may use your version of this file under
 * either the BSD or the GPL.
 */

#ifndef EVENT_H_
#define EVENT_H_

#include "ev.h"

struct timeval;
struct event_base;

#define EVLIST_TIMEOUT  0x01
#define EVLIST_INSERTED 0x02
#define EVLIST_SIGNAL   0x04
#define EVLIST_ACTIVE   0x08
#define EVLIST_INTERNAL 0x10
#define EVLIST_INIT     0x80
#define EV_PERSIST                 0x10
#define EVLOOP_ONCE      EVLOOP_ONESHOT

struct event
{
  /* libev watchers we map onto */
  union {
    struct ev_io io;
    struct ev_signal sig;
  } iosig;
  struct ev_timer to;

  /* compatibility slots */
  struct event_base *ev_base;
  void (*ev_callback)(int, short, void *arg);
  void *ev_arg;
  int ev_fd;
  int ev_pri;
  int ev_res;
  int ev_flags;
  short ev_events;
};

/*#define event_initialized(ev)      ((ev)->ev_flags & EVLIST_INIT)*/
/*#define evtimer_set(ev,cb,data)    event_set (ev, -1, 0, cb, data)*/
/*const char *event_get_version (void);*/
/*const char *event_get_method (void);*/
/*#define _EVENT_LOG_DEBUG 0*/
/*#define _EVENT_LOG_MSG   1*/
/*#define _EVENT_LOG_WARN  2*/
/*#define _EVENT_LOG_ERR   3*/
/*void event_base_free (struct event_base *base);*/
/*int event_base_set (struct event_base *base, struct event *ev);*/
/*int event_base_loop (struct event_base *base, int);*/
/*int event_base_loopexit (struct event_base *base, struct timeval *tv);*/
/*int event_base_dispatch (struct event_base *base);*/
/*int event_base_once (struct event_base *base, int fd, short events, void (*cb)(int, short, void *), void *arg, struct timeval *tv);*/
/*int event_base_priority_init (struct event_base *base, int fd);*/
/*int event_loopexit (struct timeval *tv);*/
/*int event_priority_init (int npri);*/
/*int event_priority_set (struct event *ev, int pri);*/
/*typedef void (*event_log_cb)(int severity, const char *msg);*/
/*void event_set_log_callback(event_log_cb cb);*/
/*void event_active (struct event *ev, int res, short ncalls);*/ /* ncalls is being ignored */
/*int event_once (int fd, short events, void (*cb)(int, short, void *), void *arg, struct timeval *tv);*/
/*int event_dispatch(void);*/  /* not crucial for Syncless */

/*void *event_init(void);*/
/*int event_loop(int);*/
void event_set(struct event *ev, int fd, short events, void (*cb)(int, short, void *), void *arg);
int event_add(struct event *ev, const struct timeval *tv);
int event_del(struct event *ev);
int event_pending(struct event *ev, short, struct timeval *tv);

const char *event_get_version(void);
const char *event_get_method(void);

struct ev_loop;  /* same as struct event_base */
struct ev_loop *ev_loop_new(unsigned int flags);  /* EVFLAG_AUTO */
void ev_loop_fork(struct ev_loop *loop);
void event_base_free(struct event_base *base);
int event_base_loop(struct event_base *base, int);

#endif
