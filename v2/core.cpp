#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <queue>
#include <utility>
#include <vector>

struct Vec {
    double x, y, z;
    Vec operator+(Vec v) const { return {x+v.x, y+v.y, z+v.z}; }
    Vec operator-(Vec v) const { return {x-v.x, y-v.y, z-v.z}; }
    Vec operator*(double s) const { return {x*s, y*s, z*s}; }
};
double dot(Vec a, Vec b) { return a.x*b.x+a.y*b.y+a.z*b.z; }
double norm(Vec a) { return std::sqrt(dot(a,a)); }
double horiz(Vec a) { return std::sqrt(a.x*a.x+a.y*a.y); }
Vec xy(Vec a) { return {a.x,a.y,0}; }
Vec up(double h) { return {0,0,h}; }
Vec heading(double yaw) { return {std::cos(yaw),std::sin(yaw),0}; }
double yaw_to(Vec v) { return std::atan2(v.y,v.x); }
constexpr double PI = 3.141592653589793;
constexpr double INF = std::numeric_limits<double>::infinity();

struct Stats {
    double radius, height, eye_height, muzzle_height;
    double walk_speed, boost_speed, ground_accel, air_accel, gravity;
    double jump_speed, jet_accel, max_rise_speed, step_up, max_slope_deg;
    double energy, energy_regen, energy_regen_delay, boost_drain, jet_drain;
};
struct State { Vec pos, vel; double energy, idle; int grounded; };
struct Control { double x,y; int boost,jump; };
struct Zone { double x,y,radius,dps; };
struct GridPoint { Vec pos, next; double travel; int roof; };

static Vec on_segment(Vec p,Vec a,Vec b) {
    Vec d=xy(b-a);
    double dd=dot(d,d);
    return a+d*(dd>0 ? std::clamp(dot(xy(p-a),d)/dd,0.0,1.0):0);
}
struct Prism {
    double base,top,x0,y0,x1,y1;
    std::vector<std::vector<Vec>> rings;
    bool inside(Vec p) const {
        bool occupied=false;
        for (const auto &ring:rings) {
            bool in=false;
            for (size_t i=0,j=ring.size()-1;i<ring.size();j=i++) {
                Vec a=ring[i], b=ring[j];
                if ((a.y>p.y)!=(b.y>p.y) && p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x) in=!in;
            }
            if (in) occupied=!occupied;
        }
        return occupied;
    }
    Vec nearest(Vec p) const {
        Vec answer={INF,INF,0};
        double best=INF;
        for (const auto &ring:rings) for(size_t i=0,j=ring.size()-1;i<ring.size();j=i++) {
            Vec q=on_segment(p,ring[i],ring[j]);
            Vec d=xy(q-p);
            double dd=dot(d,d);
            if(dd<best) { best=dd; answer=q; }
        }
        return answer;
    }
    bool overlap(Vec p,double radius) const {
        if(p.x<x0-radius||p.x>x1+radius||p.y<y0-radius||p.y>y1+radius) return false;
        if(inside(p)) return true;
        if(radius<=0) return false;
        Vec delta=xy(nearest(p)-p);
        return dot(delta,delta)<radius*radius-1e-8;
    }
    double hit(Vec a,Vec b) const {
        Vec d=b-a;
        double lo=0,hi=1;
        auto clip=[&](double v,double dv,double floor,double ceil){
            if(std::abs(dv)<1e-9) return v>=floor&&v<=ceil;
            double x=(floor-v)/dv,y=(ceil-v)/dv;
            lo=std::max(lo,std::min(x,y)); hi=std::min(hi,std::max(x,y));
            return lo<=hi;
        };
        if(!clip(a.x,d.x,x0,x1)||!clip(a.y,d.y,y0,y1)||!clip(a.z,d.z,base,top)) return INF;
        auto cross=[](Vec u,Vec v){return u.x*v.y-u.y*v.x;};
        Vec at=a+d*lo;
        if(inside(at+d*1e-8)) return lo;
        double first=INF;
        for(const auto &r:rings) for(size_t i=0,j=r.size()-1;i<r.size();j=i++) {
            Vec e=r[i]-r[j], w=r[j]-a;
            double det=cross(d,e);
            if(std::abs(det)<1e-10) continue;
            double t=cross(w,e)/det, u=cross(w,d)/det;
            if(t>=lo-1e-9&&t<=hi+1e-9&&u>=0&&u<=1) first=std::min(first,t);
        }
        return first;
    }
};
struct Tree { Vec pos; double crown, low, high, trunk; };
struct Nav {
    Vec pos;
    std::array<int,8> near{};
    int layer;
};
struct World {
    double x0,y0,x1,y1,tx,ty,ts,scale;
    int nx,ny,bx,by,gx=0,gy=0;
    double grid_x=0,grid_y=0;
    std::vector<int16_t> terrain;
    std::deque<Prism> prisms;
    std::vector<std::vector<const Prism*>> bins;
    std::vector<Tree> trees;
    std::vector<std::vector<size_t>> tree_bins;
    double largest_tree=0;
    std::vector<Nav> nav;
    std::vector<std::array<int,2>> grid;
    static constexpr int cell=24;
    static constexpr int spacing=4;
    int ix(double x) const {return std::clamp(int((x-x0)/cell),0,bx-1);}
    int iy(double y) const {return std::clamp(int((y-y0)/cell),0,by-1);}
    template<class F> void visit(double l,double d,double r,double u,F fn) const {
        for(int j=iy(d);j<=iy(u);++j) for(int i=ix(l);i<=ix(r);++i)
            for(auto p:bins[j*bx+i]) fn(*p);
    }
    double ground(double x,double y) const {
        double fx=std::clamp((x-tx)/ts,0.0,nx-1.0),fy=std::clamp((y-ty)/ts,0.0,ny-1.0);
        int i=std::min(int(fx),nx-2),j=std::min(int(fy),ny-2);
        fx-=i; fy-=j;
        size_t n=j*nx+i;
        return scale*((1-fy)*((1-fx)*terrain[n]+fx*terrain[n+1])+
                      fy*((1-fx)*terrain[n+nx]+fx*terrain[n+nx+1]));
    }
    double floor(Vec p,double ceiling,double radius=0) const {
        double h=ground(p.x,p.y);
        visit(p.x-radius,p.y-radius,p.x+radius,p.y+radius,[&](const Prism &b){
            if(b.top<=ceiling&&b.top>h&&b.overlap(p,radius)) h=b.top;
        });
        return h;
    }
    const Prism *roof_at(Vec p) const {
        const Prism *roof=nullptr;
        const double height=p.z-ground(p.x,p.y);
        if(height<5.8||height>14.2) return nullptr;
        visit(p.x,p.y,p.x,p.y,[&](const Prism &candidate){
            if(!roof&&std::abs(p.z-candidate.top)<.12&&candidate.inside(p)) roof=&candidate;
        });
        return roof;
    }
    const Prism* wall(Vec p,const Stats &s,double bottom) const {
        const Prism *hit=nullptr;
        visit(p.x-s.radius,p.y-s.radius,p.x+s.radius,p.y+s.radius,[&](const Prism &b){
            if(!hit&&b.top>bottom+1e-5&&b.base<p.z+s.height-1e-5&&b.overlap(p,s.radius)) hit=&b;
        });
        return hit;
    }
    double ray(Vec a,Vec b) const {
        double best=INF;
        const auto low=std::min(a.z,b.z),high=std::max(a.z,b.z);
        visit(std::min(a.x,b.x),std::min(a.y,b.y),std::max(a.x,b.x),std::max(a.y,b.y),[&](const Prism &p){
            if(p.base<=high&&p.top>=low) best=std::min(best,p.hit(a,b));
        });
        int steps=std::max(1,int(std::ceil(horiz(b-a)/2.0)));
        for(int i=0;i<=steps&&double(i)/steps<best;++i) {
            double t=double(i)/steps;
            Vec p=a+(b-a)*t;
            if(p.z<ground(p.x,p.y)-1e-5) {
                double l=std::max(0.0,double(i-1)/steps);
                for(int n=0;n<5;++n) {
                    double m=(t+l)/2; Vec q=a+(b-a)*m;
                    if(q.z<ground(q.x,q.y)) t=m; else l=m;
                }
                return t;
            }
        }
        return best;
    }
    bool clear(Vec a,Vec b) const {
        Vec d=b-a;
        const double xlo=std::min(a.x,b.x), xhi=std::max(a.x,b.x);
        const double ylo=std::min(a.y,b.y), yhi=std::max(a.y,b.y);
        const double low=std::min(a.z,b.z),high=std::max(a.z,b.z);
        const double half=cell*.5, span=half*(std::abs(d.x)+std::abs(d.y))+1e-8;
        for(int j=iy(ylo);j<=iy(yhi);++j) for(int i=ix(xlo);i<=ix(xhi);++i) {
            if(std::abs(d.x*(y0+j*cell+half-a.y)-d.y*(x0+i*cell+half-a.x))>span) continue;
            for(const Prism *p:bins[j*bx+i]) {
                if(p->base<=high&&p->top>=low&&std::isfinite(p->hit(a,b))) return false;
            }
        }
        int steps=std::max(1,int(std::ceil(horiz(d)/2)));
        for(int i=0;i<=steps;++i) {
            Vec p=a+d*(double(i)/steps);
            if(p.z<ground(p.x,p.y)-1e-5) return false;
        }
        return true;
    }
    double vision(Vec a,Vec b) const {
        if(!clear(a,b)) return 0;
        double transmission=1;
        Vec d=b-a;
        int i0=ix(std::min(a.x,b.x)-largest_tree),i1=ix(std::max(a.x,b.x)+largest_tree);
        int j0=iy(std::min(a.y,b.y)-largest_tree),j1=iy(std::max(a.y,b.y)+largest_tree);
        double span=std::max(1e-8,dot(xy(d),xy(d)));
        for(int j=j0;j<=j1;++j) for(int i=i0;i<=i1;++i) {
            for(size_t index:tree_bins[j*bx+i]) {
                const auto &tree=trees[index];
                Vec p=on_segment(tree.pos,a,b);
                double distance=horiz(tree.pos-p);
                if(distance>=std::max(tree.crown,tree.trunk)) continue;
                double t=dot(xy(p-a),xy(d))/span;
                double h=a.z+d.z*t;
                if(h<tree.high&&h>tree.pos.z&&distance<tree.trunk) return 0;
                if(h>=tree.low&&h<=tree.high&&distance<tree.crown)
                    transmission*=1-0.55*std::sqrt(std::max(0.0,1-distance*distance/(tree.crown*tree.crown)));
            }
        }
        return transmission;
    }

    State step(State m,Control c,const Stats &s,double dt,double speed_scale) const {
        Vec movement{c.x,c.y,0};
        double input=horiz(movement);
        if(input>1) movement=movement*(1/input);
        if(c.jump&&m.grounded) { m.vel.z=s.jump_speed; m.grounded=0; }
        bool spending=c.boost&&m.energy>0&&(!m.grounded||input>0.01);
        double speed=m.grounded&&spending?s.boost_speed:s.walk_speed;
        Vec change=movement*(speed*speed_scale)-xy(m.vel);
        double limit=(m.grounded?s.ground_accel:s.air_accel)*dt;
        double delta=horiz(change);
        if(delta>limit) change=change*(limit/delta);
        m.vel=m.vel+change;
        if(spending) {
            m.idle=0;
            m.energy=std::max(0.0,m.energy-dt*(m.grounded?s.boost_drain:s.jet_drain));
        } else {
            m.idle+=dt;
            if(m.idle>=s.energy_regen_delay) m.energy=std::min(s.energy,m.energy+s.energy_regen*dt);
        }
        if(!m.grounded) {
            m.vel.z-=s.gravity*dt;
            if(spending) m.vel.z=std::min(s.max_rise_speed,m.vel.z+s.jet_accel*dt);
        }
        Vec from=m.pos, horizontal=xy(m.vel)*dt;
        auto position=[&](Vec d) {
            Vec q=from+d;
            q.x=std::clamp(q.x,x0+s.radius,x1-s.radius);
            q.y=std::clamp(q.y,y0+s.radius,y1-s.radius);
            return q;
        };
        double raised=m.grounded?s.step_up:0;
        const double h0=ground(from.x,from.y), slope=std::tan(s.max_slope_deg*PI/180);
        auto allowed=[&](Vec q) {
            const double h1=ground(q.x,q.y);
            const double rise=horiz(q-from)*slope;
            if(h1>std::max(from.z,h0+rise)+1e-6) return false;
            if(raised>0) q.z=std::max(q.z,floor(q,q.z+raised));
            return !wall(q,s,q.z);
        };
        Vec q=position(horizontal);
        if(!allowed(q)) {
            const Prism *b=wall(q,s,q.z+raised);
            bool slid=false;
            if(b) {
                Vec normal=xy(from-b->nearest(q));
                double length=horiz(normal);
                if(length>1e-7) {
                    normal=normal*(1/length);
                    Vec projected=position(horizontal-normal*std::min(0.0,dot(horizontal,normal)));
                    if(allowed(projected)) { q=projected; slid=true; }
                }
            }
            if(!slid) {
                Vec a=position({horizontal.x,0,0}), y=position({0,horizontal.y,0});
                bool ax=allowed(a), ay=allowed(y);
                q=ax&&(!ay||std::abs(horizontal.x)>=std::abs(horizontal.y))?a:ay?y:from;
            }
        }
        m.vel.x=(q.x-from.x)/dt; m.vel.y=(q.y-from.y)/dt;
        double old=m.pos.z, next=old+m.vel.z*dt;
        const Prism *eave=nullptr;
        if(next>old) visit(q.x-s.radius,q.y-s.radius,q.x+s.radius,q.y+s.radius,[&](const Prism &p){
            double underside=p.base-s.height;
            if(underside>=old-1e-5&&underside<next&&p.overlap(q,s.radius)) {
                next=std::max(old,underside); m.vel.z=0; eave=&p;
            }
        });
        if(eave&&!eave->inside(q)) {
            Vec outward=xy(q-eave->nearest(q));
            double d=horiz(outward);
            if(d>1e-7&&d<s.radius) {
                Vec slide=q+outward*(std::min(.6,s.radius-d+1e-4)/d);
                if(slide.x>=x0+s.radius&&slide.x<=x1-s.radius&&slide.y>=y0+s.radius&&slide.y<=y1-s.radius
                   &&allowed(slide)) q=slide;
            }
        }
        double landing=floor(q,old+(m.grounded?s.step_up:0)+1e-6);
        if(!m.grounded&&m.vel.z<=0&&next<=landing+s.step_up)
            landing=std::max(landing,floor(q,old+1e-6,s.radius));
        if(next<=landing+1e-5||(m.grounded&&old-landing<0.08)) {
            q.z=landing; m.vel.z=0; m.grounded=1;
        } else {
            if(m.grounded) { m.vel.z=-s.gravity*dt; next=old+m.vel.z*dt; }
            q.z=next; m.grounded=0;
        }
        m.pos=q;
        return m;
    }

    std::array<int,2> nodes(Vec p) const {
        int i=int(std::round((p.x-grid_x)/spacing)),j=int(std::round((p.y-grid_y)/spacing));
        if(i<0||i>=gx||j<0||j>=gy) return {-1,-1};
        return grid[j*gx+i];
    }
    void build(const Stats &s) {
        grid_x=std::ceil((x0+s.radius)/spacing)*spacing;
        grid_y=std::ceil((y0+s.radius)/spacing)*spacing;
        gx=std::max(0,1+int(std::floor((x1-s.radius-grid_x)/spacing)));
        gy=std::max(0,1+int(std::floor((y1-s.radius-grid_y)/spacing)));
        grid.assign(gx*gy,{-1,-1}); nav.clear();
        for(int j=0;j<gy;++j) for(int i=0;i<gx;++i) {
            Vec p{grid_x+i*spacing,grid_y+j*spacing,0};
            p.z=ground(p.x,p.y);
            if(!wall(p,s,p.z+s.step_up)) {
                grid[j*gx+i][0]=int(nav.size());
                nav.push_back({p,{},0});
            }
            double top=floor(p,p.z+14,0);
            if(top-p.z>=6&&top-p.z<=14&&!wall({p.x,p.y,top},s,top)) {
                grid[j*gx+i][1]=int(nav.size()); nav.push_back({{p.x,p.y,top},{},1});
            }
        }
        const std::array<Vec,8> offsets{{{4,0,0},{-4,0,0},{0,4,0},{0,-4,0},{4,4,0},{4,-4,0},{-4,4,0},{-4,-4,0}}};
        for(auto &point:nav) {
            point.near.fill(-1);
            for(size_t i=0;i<offsets.size();++i) {
                Vec next=point.pos+offsets[i];
                int id=nodes(next)[point.layer];
                if(id<0) continue;
                Vec dest=nav[id].pos;
                if(std::abs(dest.z-point.pos.z)>std::max(s.step_up,horiz(offsets[i])*std::tan(s.max_slope_deg*PI/180))) continue;
                Vec middle=(point.pos+dest)*0.5;
                if(wall(middle,s,middle.z+s.step_up)) continue;
                if(point.layer&&floor(middle,middle.z+0.2)<middle.z-0.4) continue;
                point.near[i]=id;
            }
        }
    }
    std::vector<GridPoint> reach(Vec start,const Stats &s,int max_nodes,double radius) const {
        int root=-1; double best=INF;
        for(int i=-2;i<=2;++i) for(int j=-2;j<=2;++j) {
            for(int node:nodes(start+Vec{double(i*spacing),double(j*spacing),0})) if(node>=0) {
                double d=norm(start-nav[node].pos);
                if(d<best&&clear(start+up(s.height*.5),nav[node].pos+up(s.height*.5))) {
                    root=node; best=d;
                }
            }
        }
        std::vector<GridPoint> result;
        if(root<0) return result;
        std::vector<double> time(nav.size(),INF);
        std::vector<int> next_node(nav.size(),-1);
        using Entry=std::pair<double,int>;
        std::priority_queue<Entry,std::vector<Entry>,std::greater<Entry>> queue;
        queue.push({0,root}); time[root]=0;
        while(!queue.empty()&&int(result.size())<max_nodes) {
            auto [t,id]=queue.top(); queue.pop();
            if(t!=time[id]) continue;
            const auto &cur=nav[id];
            result.push_back({cur.pos,next_node[id]>=0?nav[next_node[id]].pos:cur.pos,t,cur.layer});
            for(int n:cur.near) if(n>=0&&horiz(nav[n].pos-start)<=radius) {
                double next=t+norm(nav[n].pos-cur.pos)/s.walk_speed;
                if(next<time[n]) {
                    time[n]=next;
                    next_node[n]=id==root?n:next_node[id];
                    queue.push({next,n});
                }
            }
        }
        return result;
    }
};

double cylinder(Vec a,Vec b,Vec base,double radius,double height) {
    Vec d=b-a,o=a-base;
    double lo=0,hi=1;
    double qa=dot(xy(d),xy(d)),qb=dot(xy(o),xy(d)),qc=dot(xy(o),xy(o))-radius*radius;
    if(qa>1e-12) {
        double disc=qb*qb-qa*qc;
        if(disc<0) return INF;
        double root=std::sqrt(disc);
        lo=std::max(lo,(-qb-root)/qa);hi=std::min(hi,(-qb+root)/qa);
    } else if(qc>0) return INF;
    if(std::abs(d.z)>1e-12) {
        double t=-o.z/d.z,u=(height-o.z)/d.z;
        lo=std::max(lo,std::min(t,u));hi=std::min(hi,std::max(t,u));
    } else if(o.z<0||o.z>height) return INF;
    return lo<=hi?lo:INF;
}

static Vec zone_center(Zone z) {return {z.x,z.y,0};}
static double outside(Vec p,Zone zone) {return std::max(0.0,horiz(p-zone_center(zone))-zone.radius);}

extern "C" {
World* world_new(const double *bounds,const double *terrain,int nx,int ny,const int16_t *heights) {
    auto w=new World{};
    w->x0=bounds[0];w->y0=bounds[1];w->x1=bounds[2];w->y1=bounds[3];
    w->tx=terrain[0];w->ty=terrain[1];w->ts=terrain[2];w->scale=terrain[3];w->nx=nx;w->ny=ny;
    w->terrain.assign(heights,heights+nx*ny);
    w->bx=1+int((w->x1-w->x0)/World::cell); w->by=1+int((w->y1-w->y0)/World::cell);
    w->bins.resize(w->bx*w->by);
    w->tree_bins.resize(w->bx*w->by);
    return w;
}
void world_free(World *w) {delete w;}
void world_prism(World *w,double base,double top,const double *xy_coords,const int *sizes,int rings) {
    Prism p{base,top,INF,INF,-INF,-INF,{}};
    int cursor=0;
    for(int i=0;i<rings;++i) {
        std::vector<Vec> ring;
        for(int j=0;j<sizes[i];++j) {
            double x=xy_coords[cursor++], y=xy_coords[cursor++];
            ring.push_back({x,y,0});
            p.x0=std::min(p.x0,x); p.x1=std::max(p.x1,x); p.y0=std::min(p.y0,y); p.y1=std::max(p.y1,y);
        }
        if(ring.size()>=3) p.rings.push_back(std::move(ring));
    }
    if(p.rings.empty()) return;
    w->prisms.push_back(std::move(p)); const auto &b=w->prisms.back();
    for(int j=w->iy(b.y0);j<=w->iy(b.y1);++j) for(int i=w->ix(b.x0);i<=w->ix(b.x1);++i)
        w->bins[j*w->bx+i].push_back(&b);
}
void world_tree(World *w, Vec p,double crown,double low,double height,double trunk) {
    w->tree_bins[w->iy(p.y)*w->bx+w->ix(p.x)].push_back(w->trees.size());
    w->trees.push_back({p,crown,p.z+low,p.z+height,trunk});
    w->largest_tree=std::max({w->largest_tree,crown,trunk});
}
void world_build(World *w,const Stats *s) {w->build(*s);}
double world_ground(World *w,double x,double y) {return w->ground(x,y);}
int world_spawnable(World *w,Vec p,const Stats *s) {return w->wall(p,*s,p.z+s->step_up)==nullptr;}
int world_on_roof(World *w,Vec p) {return w->roof_at(p)!=nullptr;}
int practice_laser_cover(World *w,const Stats *s,const State *start,Zone zone,Vec source,
                         double dt,double speed_scale,int boost,Control *out) {
    if(!start->grounded||!w->clear(source,start->pos+up(s->height*.5))) return 0;
    int best_steps=int(std::round(.4/dt))+1;
    const double facing=yaw_to(source-start->pos);
    for(int i=0;i<16;++i) {
        const double yaw=facing+(i%2==0?1:-1)*(PI/2+i/2*PI/8);
        const Control move={std::cos(yaw),std::sin(yaw),boost,0};
        State state=*start;
        for(int j=1;j<best_steps;++j) {
            state=w->step(state,move,*s,dt,speed_scale);
            if(!state.grounded||horiz(state.pos-start->pos)>4.01||outside(state.pos,zone)>0) break;
            const Vec chest=state.pos+up(s->height*.5);
            const Vec side=xy(source-state.pos);
            const double length=horiz(side);
            if(length<1.) continue;
            const Vec edge=Vec{-side.y,side.x,0}*(s->radius*.7/length);
            if(!w->clear(source,chest)&&!w->clear(source,chest+edge)&&!w->clear(source,chest-edge)
                                       &&!w->clear(source,state.pos+up(s->muzzle_height))) {
                *out=move;
                best_steps=j;
                break;
            }
        }
    }
    return best_steps<=int(std::round(.4/dt));
}

double world_ray(World *w,Vec a,Vec b) {return w->ray(a,b);}
double world_vision(World *w,Vec a,Vec b) {return w->vision(a,b);}
void world_rays(World *w,const Vec *a,const Vec *b,double *out,int n,int optical) {
    for(int i=0;i<n;++i) out[i]=optical?w->vision(a[i],b[i]):w->ray(a[i],b[i]);
}
double body_hit(Vec a,Vec b,Vec p,double radius,double height) {return cylinder(a,b,p,radius,height);}
void step_mech(World *w,const Stats *s,State *m,const Control *c,double dt,double speed_scale) {
    *m=w->step(*m,*c,*s,dt,speed_scale);
}
int world_reach(World *w,Vec p,const Stats *s,GridPoint *out,int n,double radius) {
    auto result=w->reach(p,*s,n,radius); std::copy(result.begin(),result.end(),out); return int(result.size());
}
}
